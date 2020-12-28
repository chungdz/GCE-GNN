#!/usr/bin/env python36
# -*- coding: utf-8 -*-

import datetime
import math
import numpy as np
import torch
from torch import nn
from torch.nn import Module, Parameter
import torch.nn.functional as F
from torch_geometric.nn import GCNConv, GATConv, GatedGraphConv, SAGEConv
from torch_geometric.data import NeighborSampler
from torch_cluster import random_walk
from tqdm import tqdm


class GNN(Module):
    def __init__(self, hidden_size, opt, n_node, step=1):
        super(GNN, self).__init__()
        self.step = step
        self.hidden_size = hidden_size
        self.input_size = hidden_size * 2
        self.gate_size = 3 * hidden_size
        self.w_ih = Parameter(torch.Tensor(self.gate_size, self.input_size))
        self.w_hh = Parameter(torch.Tensor(self.gate_size, self.hidden_size))
        self.b_ih = Parameter(torch.Tensor(self.gate_size))
        self.b_hh = Parameter(torch.Tensor(self.gate_size))
        self.b_iah = Parameter(torch.Tensor(self.hidden_size))
        self.b_oah = Parameter(torch.Tensor(self.hidden_size))

        self.linear_edge_in = nn.Linear(self.hidden_size, self.hidden_size, bias=True)
        self.linear_edge_out = nn.Linear(self.hidden_size, self.hidden_size, bias=True)
        self.linear_edge_f = nn.Linear(self.hidden_size, self.hidden_size, bias=True)

        heads = 1
        self.conv1 = GATConv(self.hidden_size, self.hidden_size, heads=heads, dropout=0.6)
        # On the Pubmed dataset, use heads=8 in conv2.
        self.conv2 = GATConv(heads * self.hidden_size, self.hidden_size, heads=1, concat=False, dropout=0.6)
        self.ggnn = GatedGraphConv(self.hidden_size, step+1)

    def GNNCell(self, A, hidden, edge_index):
        input_in = torch.matmul(A[:, :, :A.shape[1]], self.linear_edge_in(hidden)) + self.b_iah
        input_out = torch.matmul(A[:, :, A.shape[1]: 2 * A.shape[1]], self.linear_edge_out(hidden)) + self.b_oah
        inputs = torch.cat([input_in, input_out], 2)
        gi = F.linear(inputs, self.w_ih, self.b_ih)
        gh = F.linear(hidden, self.w_hh, self.b_hh)
        i_r, i_i, i_n = gi.chunk(3, 2)
        h_r, h_i, h_n = gh.chunk(3, 2)
        resetgate = torch.sigmoid(i_r + h_r)
        inputgate = torch.sigmoid(i_i + h_i)
        newgate = torch.tanh(i_n + resetgate * h_n)
        hy = newgate + inputgate * (hidden - newgate)
        return hy

    def forward(self, A, hidden, edge_index):
        # 原始 paper用 GAT取代 GGNN
        hidden = F.relu(self.conv1(hidden, edge_index))
        # hidden = self.conv2(hidden, edge_index)

        # GGNN layer
        # hidden = F.relu(hidden)
        hidden = F.relu(self.ggnn(hidden, edge_index))

        # for i in range(self.step):
        #     hidden = self.GNNCell(A, hidden, edge_index)
        return hidden


class GlobalGraph(Module):
    def __init__(self, opt, n_node):
        super(GlobalGraph, self).__init__()
        self.hidden_size = opt.hiddenSize
        in_channels = hidden_channels = self.hidden_size
        self.num_layers = 2
        heads = 1
        # todo 暫時用GAT
        self.gat = GATConv(self.hidden_size, self.hidden_size, heads=heads, dropout=0.3)
        self.convs = nn.ModuleList()
        # todo Aggregation/ MessagePassing, 暫時用graphsage (mean)
        self.convs.append(SAGEConv(in_channels, hidden_channels, normalize=True))
        for i in range(self.num_layers -1):
            self.convs.append(SAGEConv(hidden_channels, hidden_channels, normalize=True))

    def forward(self, x, adjs):
        xs = []
        x_all = x
        # 每個session graph分別過gat, sage
        if self.num_layers > 1:
            for i, (edge_index, _, size) in enumerate(adjs):
                x = x_all
                if len(list(x.shape)) < 2:
                    x = x.unsqueeze(0)  # add one more dim to wrap the embedding
                x = self.gat(x, edge_index)  # 加gat
                # sage
                x_target = x[:size[1]]  # Target nodes are always placed first.
                x = self.convs[i]((x, x_target), edge_index)
                if i != self.num_layers - 1:
                    x = F.relu(x)  # 最後一曾不用relu
        else:
            # 只有 1-hop的情況
            edge_index, size = adjs.edge_index, adjs.size
            x = x_all
            if len(list(x.shape)) < 2:
                x = x.unsqueeze(0)  # add one more dim to wrap the embedding
            x = self.gat(x, edge_index)  # 加gat
            x_target = x[:size[1]]  # Target nodes are always placed first.
            x = self.convs[-1]((x, x_target), edge_index)  # sage
        xs.append(x)
        return torch.cat(xs, 0)


class SessionGraph(Module):
    def __init__(self, opt, n_node):
        super(SessionGraph, self).__init__()
        self.hidden_size = opt.hiddenSize
        self.global_data = torch.load('./datasets/'+opt.dataset+'/global_graph_start0.pt')
        self.n_node = n_node
        self.batch_size = opt.batchSize
        self.nonhybrid = opt.nonhybrid
        self.embedding = nn.Embedding(self.n_node, self.hidden_size)
        self.embedding2 = nn.Embedding(self.n_node, self.hidden_size)
        self.gnn = GNN(self.hidden_size, opt, n_node, step=opt.step)
        self.linear_one = nn.Linear(self.hidden_size, self.hidden_size, bias=True)
        self.linear_two = nn.Linear(self.hidden_size, self.hidden_size, bias=True)
        self.linear_three = nn.Linear(self.hidden_size, 1, bias=False)
        self.linear_transform = nn.Linear(self.hidden_size * 2, self.hidden_size, bias=True)
        self.gru = nn.GRU(self.hidden_size, self.hidden_size, num_layers=1, dropout=0, batch_first=True)
        self.global_g = GlobalGraph(opt, n_node)
        self.loss_function = nn.CrossEntropyLoss()
        self.optimizer = torch.optim.Adam(self.parameters(), lr=opt.lr, weight_decay=opt.l2)
        self.scheduler = torch.optim.lr_scheduler.StepLR(self.optimizer, step_size=opt.lr_dc_step, gamma=opt.lr_dc)
        self.reset_parameters()

    def reset_parameters(self):
        stdv = 1.0 / math.sqrt(self.hidden_size)
        for weight in self.parameters():
            weight.data.uniform_(-stdv, stdv)

    def compute_scores(self, hidden, mask=None, use_mask=True):
        if use_mask:
            hidden, _ = self.gru(hidden)
            ht = hidden[torch.arange(mask.shape[0]).long(), torch.sum(mask, 1) - 1]  # batch_size x latent_size
            # todo 可以加 TAGNN的想法
            q1 = self.linear_one(ht).view(ht.shape[0], 1, ht.shape[1])  # batch_size x 1 x latent_size
            q2 = self.linear_two(hidden)  # batch_size x seq_length x latent_size
            alpha = self.linear_three(torch.sigmoid(q1 + q2))
            a = torch.sum(alpha * hidden * mask.view(mask.shape[0], -1, 1).float(), 1)
            if not self.nonhybrid:
                a = self.linear_transform(torch.cat([a, ht], 1))
            # todo 改用 node embedding
            b = self.embedding.weight[1:]  # n_nodes x latent_size
            scores = torch.matmul(a, b.transpose(1, 0))
        return scores

    def forward(self, inputs, A, edge_index=None, g_samplers=None, unique=False):
        hidden = self.embedding(inputs).squeeze()  # 把node id轉成embedding
        hidden = self.gnn(A, hidden, edge_index)  # session graph

        # call global graph
        g_adjs = []
        n_idxs = []
        s_nodes = []  # 每個session內node的embedding
        for (b_size, node_idx, adjs) in g_samplers:
            if type(adjs) == list:  # 2-hop
                g_adjs = [adj.to('cuda') for adj in adjs]
            else:
                g_adjs = adjs.to('cuda')
            n_idxs = node_idx.cuda()  # 過完global拿到的shape會和放進去的一樣
            # s_nodes = self.embedding2(n_idxs).squeeze()  # global graph的 item emb獨立效果也差不多
            s_nodes = self.embedding(n_idxs).squeeze()  # global graph的 item emb獨立效果也差不多

        g_hidden = self.global_g(s_nodes, g_adjs)  # nodes轉成embedding丟進global graph
        # g__ = g_hidden.cpu().detach().numpy()  # same node_idx會拿到一樣的g_hidden

        if unique:
            # 從inputs的id取g_hidden的emb
            indices = []  # 建所有對照的位置, 最後一次tensor select
            for i in inputs:
                indices.append((n_idxs==i).nonzero(as_tuple=False)[0][0])  # 紀錄第一個出現的位置
            indices = torch.tensor(indices).cuda()
            g_h = torch.index_select(g_hidden, 0, indices)
        else:
            g_h = g_hidden
        # hidden += g_h
        pad = self.embedding(torch.Tensor([0]).to(torch.int64).cuda())
        return hidden, pad, g_h


def trans_to_cuda(variable):
    if torch.cuda.is_available():
        return variable.cuda()
    else:
        return variable


def trans_to_cpu(variable):
    if torch.cuda.is_available():
        return variable.cpu()
    else:
        return variable


def get(padding_emb, i, hidden, alias_inputs, length):
    # 手動padding
    h_ = hidden[i][alias_inputs[i]]
    if h_.shape[0] == length:
        return h_
    else:
        r_ = padding_emb.repeat(length - h_.shape[0], 1)
        return torch.cat([h_, r_])


def forward(model, i, data):
    # 改成用 geometric的Data格式
    items, targets, mask, batch, seq = data.x, data.y, data.sequence_mask, data.batch, data.sequence
    seq = seq.view(targets.shape[0], -1)
    mask = mask.view(targets.shape[0], -1)

    A = []
    # datas = data.to_data_list()
    # graphs = [to_networkx(d) for d in datas]
    # A = [nx.convert_matrix.to_pandas_adjacency(g).values for g in graphs]  # 無向圖adj = in + out
    # A_out = [g for g in graphs]  # 有向圖的adj就是A_out

    # todo 解決cpu usage高的問題
    # global graph
    gg = model.global_data
    gg_edge_index = gg.edge_index
    # 直接對 batch下所有node做NeighborSample
    batch_nodes = seq.flatten()
    # batch_nodes = torch.unique(batch_nodes)  # 取unique node in batch sessions
    # batch_nodes = batch_nodes[batch_nodes!=0]  # 移除padding node id
    # sample as whole batch, 從大graph中找session graph內的node id的鄰居
    # subgraph_loaders = NeighborSampler(gg_edge_index, node_idx=batch_nodes, sizes=[-1], shuffle=False, num_workers=0, batch_size=batch_nodes.shape[0])  # all neighbors
    # fixme 放全部node
    subgraph_loaders = NeighborSampler(gg_edge_index, node_idx=batch_nodes, sizes=[10, 5], shuffle=False, num_workers=0, batch_size=batch_nodes.shape[0])  # 2 hop

    hidden, pad, g_h = model(items, A, data.edge_index, subgraph_loaders)  # session graph node embeddings
    # 推回原始序列
    sections = torch.bincount(batch).cpu().numpy()
    # split whole x back into graphs G_i
    hidden = torch.split(hidden, tuple(sections))

    # todo 增加不考慮padding的選項
    mask_true = True
    if mask_true:
        leng = mask.shape[1]  # padding完的session長度
        alias_inputs = data.alias_inputs
        s_len = data.sequence_len.cpu().numpy().tolist()
        alias_inputs = torch.split(alias_inputs, s_len)
        seq_hidden = torch.stack([get(pad, i, hidden, alias_inputs, leng) for i in torch.arange(len(alias_inputs)).long()])
        g_h = g_h.view([len(hidden), leng, -1])
    else:
        seq_hidden = hidden
    seq_hidden += g_h
    return targets, model.compute_scores(seq_hidden, mask, mask_true)


def train_test(model, train, test, logging):
    print('start training: ', datetime.datetime.now())
    model.train()
    total_loss = 0.0
    for i, batch in tqdm(enumerate(train)):
        model.optimizer.zero_grad()
        targets, scores = forward(model, i, batch.to('cuda'))
        loss = model.loss_function(scores, targets - 1)
        loss.backward()
        model.optimizer.step()
        total_loss += loss
        # 控制總共要印幾次loss
        if i % int(len(train) / 5 + 1) == 0:
            print('[%d/%d] Loss: %.4f' % (i, len(train), loss.item()))
            logging.info('[%d/%d] Loss: %.4f' % (i, len(train), loss.item()))
    print('\tLoss:\t%.3f' % total_loss)
    logging.info('\tLoss:\t%.3f' % total_loss)

    print('start predicting: ', datetime.datetime.now())
    model.eval()
    hit, mrr = [], []
    for i, batch in enumerate(test):
        targets, scores = forward(model, i, batch.to('cuda'))
        targets -= 1
        sub_scores = scores.topk(20)[1]
        sub_scores = trans_to_cpu(sub_scores).detach().numpy()
        targets = targets.cpu().numpy()  # target & score must both be numpy arrays
        for score, target in zip(sub_scores, targets):
            hit.append(np.isin(target, score))
            if not np.isin(target, score):
            # if len(np.where(score == target)[0]) == 0:
                mrr.append(0)
            else:
                # at_where = np.where(score == target)
                mrr.append(1 / (np.where(score == target)[0][0] + 1))
    hit = np.mean(hit) * 100
    mrr = np.mean(mrr) * 100
    model.scheduler.step()
    return hit, mrr
