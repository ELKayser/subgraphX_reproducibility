import os
import random

import networkx as nx
import numpy as np
import torch
import torch_geometric.datasets
from matplotlib import pyplot as plt
from torch import optim
from torch.nn import ReLU, Linear, Softmax
from torch.optim import Adam
from torch_geometric.data import Data
from torch_geometric.loader import DataLoader
from torch_geometric.nn import GCNConv, Sequential

from src.algorithm.subgraph_x import SubgraphX
from src.utils.metrics import sparsity, fidelity
from src.utils.task_enum import Task
from src.utils.training import train_emb, train_model, test
from src.utils.utils import get_device, set_seed

batch_size = 1  # there is only a single graph in the dataset
num_classes = 4
num_nodes = 34

def get_model():
    device = get_device()

    input_dim = 34
    hidden_dim = 20
    output_dim = 4

    model = Sequential(
        'x, edge_index, batch', [
            (GCNConv(input_dim, hidden_dim), 'x, edge_index -> x'),
            ReLU(inplace=True),
            (GCNConv(hidden_dim, output_dim), 'x, edge_index -> x'),
            Softmax(dim=1),
        ]
    ).to(device)

    return model

def prepare_dataset(emb_model):
    dataset = torch_geometric.datasets.KarateClub()
    graph = dataset.data
    train_mask = graph.train_mask
    y = graph.y

    """
    distribution of nodes in train, test, val:
    class 0: 1 train, 6 val, 6 test
    class 1: 1 train, 6 val, 5 test
    class 2: 1 train, 2 val, 1 test
    class 3: 1 train, 2 val, 2 test
    """

    val_0_idx = random.sample(range(12), k=6)
    test_0_idx = list(set(range(12)) - set(val_0_idx))
    val_1_idx = random.sample(range(11), k=6)
    test_1_idx = list(set(range(11)) - set(val_1_idx))
    val_2_idx = random.sample(range(3), k=2)
    test_2_idx = list(set(range(3)) - set(val_2_idx))
    val_3_idx = random.sample(range(4), k=2)
    test_3_idx = list(set(range(4)) - set(val_3_idx))

    val_indices = [val_0_idx, val_1_idx, val_2_idx, val_3_idx]
    test_indices = [test_0_idx, test_1_idx, test_2_idx, test_3_idx]

    val_mask = torch.zeros_like(train_mask).bool()
    test_mask = torch.zeros_like(train_mask).bool()

    for class_i in range(num_classes):
        counter = 0  # the number of nodes of this class already processed
        cur_val_idx = val_indices[class_i]
        cur_test_idx = test_indices[class_i]

        for node in range(num_nodes):  # assign nodes to test and val mask
            node_class = y[node].item()
            if node_class == class_i and not train_mask[node]:
                # node is either in test or val mask
                if counter in cur_val_idx:
                    val_mask[node] = True
                else:
                    test_mask[node] = True
                counter += 1

    new_x = emb_model.forward(batch=np.arange(num_nodes)).cpu().detach()

    new_graph = Data(x=new_x, edge_index=graph.edge_index, y=y, train_mask=train_mask, val_mask=val_mask,
                     test_mask=test_mask)
    train_loader = DataLoader([new_graph], batch_size=batch_size, shuffle=True)
    val_loader = DataLoader([new_graph], batch_size=batch_size, shuffle=False)
    test_loader = DataLoader([new_graph], batch_size=batch_size, shuffle=False)
    return new_graph, train_loader, val_loader, test_loader

def get_embedding_model(graph):
    device = get_device()
    embedding_dim = 10
    walk_length = 80
    context_size = 10
    walks_per_node = 10

    model = torch_geometric.nn.Node2Vec(graph.edge_index, embedding_dim=embedding_dim, walk_length=walk_length,
                                        context_size=context_size, walks_per_node=walks_per_node,
                                        num_nodes=num_nodes).to(device)
    loader = model.loader(num_workers=0, batch_size=64, shuffle=True)
    return model, loader

def train_or_load_embedding(graph):
    save_path = './checkpoints/karate_club/emb'
    emb_model, emb_loader = get_embedding_model(graph)

    if os.path.isfile(save_path):  # if checkpoint exists, load it
        emb_model.load_state_dict(torch.load(save_path))
    else:
        emb_optimizer = Adam(emb_model.parameters())
        num_epochs = 2000
        train_emb(emb_model, emb_optimizer, emb_loader, num_epochs, output_freq=100)
        torch.save(emb_model.state_dict(), save_path)
    return emb_model

def get_gcn_model():
    device = get_device()
    input_dim = 10
    hidden_dim = 20
    output_dim = 4

    model = Sequential(
        'x, edge_index, batch', [
            (GCNConv(input_dim, hidden_dim), 'x, edge_index -> x'),
            ReLU(inplace=True),
            (GCNConv(hidden_dim, output_dim), 'x, edge_index -> x'),
            # Softmax(dim=1),
        ]
    ).to(device)

    return model

def train_or_load_gcn(train_loader, val_loader):
    save_path = './checkpoints/karate_club/gcn.pt'
    model = get_gcn_model()
    loss_func = torch.nn.CrossEntropyLoss()
    device = get_device()

    if os.path.isfile(save_path):
        model.load_state_dict(torch.load(save_path))
    else:
        optimizer = optim.Adam(model.parameters(), lr=0.001)
        num_epochs = 1000
        output_freq = 50
        train_model(model, False, optimizer, train_loader, val_loader, num_epochs, loss_func, save_path, output_freq,
                    task=Task.NODE_CLASSIFICATION)

    # cross entropy loss already contains softmax, therefore just add softmax layer after training
    result_model = Sequential(
        'x, edge_index, batch', [
            (model, 'x, edge_index, batch -> x'),
            Softmax(dim=1),
        ]
    ).to(device)

    # model.add_module('module_3', Softmax(dim=1))

    return result_model, loss_func

def debug(model, test_loader):
    device = get_device()
    # get scores for test set
    test_graph = next(iter(test_loader))
    test_mask = test_graph.test_mask

    scores = model(test_graph.x.to(device), test_graph.edge_index.to(device),
                   test_graph.batch.to(device)).detach().cpu()
    pred = torch.argmax(scores, dim=1)

    test_node_idx = torch.arange(0, num_nodes)[test_mask]
    test_scores = scores[test_mask]
    test_pred = pred[test_mask]
    truth = test_graph.y[test_mask]

    node = test_node_idx[0].item()
    print(f'testing explanation for node {node}')
    subgraphx = SubgraphX(model, num_layers=2, exp_weight=5, m=50, t=50, task=Task.NODE_CLASSIFICATION)
    explanation_set, mcts = subgraphx(test_graph, n_min=10, nodes_to_keep=[node])
    print(f'explanation: {explanation_set}')

    sparsity_score = sparsity(test_graph, explanation_set)
    print(f'sparsity: {sparsity_score}')

    fidelity_score = fidelity(test_graph, explanation_set, model, task=Task.NODE_CLASSIFICATION, index_of_interest=node)
    print(f'fidelity: {fidelity_score}')


def debug_2(model, test_loader):
    device = get_device()
    # get scores for test set
    test_graph = next(iter(test_loader))
    test_mask = test_graph.test_mask

    scores = model(test_graph.x.to(device), test_graph.edge_index.to(device),
                   test_graph.batch.to(device)).detach().cpu()
    pred = torch.argmax(scores, dim=1)

    test_node_idx = torch.arange(0, num_nodes)[test_mask]
    test_scores = scores[test_mask]
    test_pred = pred[test_mask]
    truth = test_graph.y[test_mask]

    node = test_node_idx[0].item()
    print(f'testing explanation for node {node}')



def main():
    dataset = torch_geometric.datasets.KarateClub()
    graph = dataset.data
    device = get_device()

    # print graph
    color_map = []
    for node in range(num_nodes):
        if graph.y[node].item() == 0:
            color_map.append('red')
        elif graph.y[node].item() == 1:
            color_map.append('blue')
        elif graph.y[node].item() == 2:
            color_map.append('green')
        elif graph.y[node].item() == 3:
            color_map.append('white')
    nx_graph = torch_geometric.utils.to_networkx(graph, to_undirected=True)
    nx.draw(nx_graph, node_color=color_map, with_labels=True)
    # plt.show()

    # first train embedding
    emb_model = train_or_load_embedding(graph)
    graph, train_loader, val_loader, test_loader = prepare_dataset(emb_model)

    # then train gcn
    model, loss_func = train_or_load_gcn(train_loader, val_loader)
    test_loss, test_acc = test(model, False, test_loader, loss_func, task=Task.NODE_CLASSIFICATION)
    print(f'test loss: {test_loss}, test_acc: {test_acc}')


    debug_2(model, test_loader)


    pass



if __name__ == '__main__':
    set_seed(0)  # IMPORTANT!!
    main()