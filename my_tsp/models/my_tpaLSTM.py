import torch
from torch import nn
import torch.nn.functional as F
import argparse
from progressbar import *
from torch.optim import Adam
import random
import matplotlib.pyplot as plt
from tqdm import tqdm
import numpy as np
import os
import pandas as pd
from datetime import date
# from util import *

class TPALSTM(nn.Module):

    def __init__(self, input_size, predict_seq_len, hidden_size, num_obs_to_train, n_layers):
        '''
        Args:
            input_size: 1
            output_horizon: args.seq_len
            hidden_size:  args.hidden_size
            obs_len: args.num_obs_to_train
            n_layers: args.n_layers
        '''
        super(TPALSTM, self).__init__()
        # args.seq_len:432,  args.hidden_size:24, a rgs.num_obs_to_train:4320,  args.n_layers:1

        # 转换维度  input_size 1 → hidden_size 24
        self.hidden = nn.Linear(input_size, hidden_size)
        self.relu = nn.ReLU()

        # lstm参数
        # input_size:    x的特征维度
        # hidden_size：   隐藏层的特征维度
        # num_layers：    隐藏层的层数，默认为1
        # bias：          默认为True
        # batch_first:    True则输出的数据格式为(batch, seq, feature)
        self.lstm = nn.LSTM(hidden_size, hidden_size, n_layers,
                            bias=True, batch_first=True)  # output (batch_size, obs_len, hidden_size)

        self.hidden_size = hidden_size   # 24
        self.predict_seq_len = predict_seq_len
        # CNN注意力参数
        self.filter_num = 32
        self.filter_size = 1
        self.attention = TemporalPatternAttention(self.filter_size,
                                                  self.filter_num, num_obs_to_train-1, hidden_size)
        # 最后的输出： hidden_size 24 → predict_seq_len
        self.linear = nn.Linear(hidden_size, predict_seq_len)
        # 隐藏层的层数
        self.n_layers = n_layers

    def forward(self, x):
        x = x.permute(1, 0).contiguous()
        # print('x1.shape:, ', x.shape)

        # [num_col, num_obs_to_train]  [2, 4320]
        batch_size, num_obs_to_train = x.size()

        # [num_col, num_obs_to_train, 1] [2, 4320, 1]
        x = x.view(batch_size, num_obs_to_train, 1)

        # xconcat: [num_col, num_obs_to_train, hidden_size] [2, 4320, 24]
        xconcat = self.relu(self.hidden(x))

        # H: [num_col, num_obs_to_train-1, hidden_size] [2, 4319, 24]
        H = torch.zeros(batch_size, num_obs_to_train-1, self.hidden_size)

        # ht: [n_layers, num_col, hidden_size]  [1, 2, 24]
        ht = torch.zeros(self.n_layers, batch_size, self.hidden_size)

        # ct: [n_layers, num_col, hidden_size]  [1, 2, 24]
        ct = ht.clone()

        for t in range(num_obs_to_train):
            # xt: [num_col, i, hidden_size]   [2, 1, 24]
            xt = xconcat[:, t, :].view(batch_size, 1, -1)   # [2, 1, 24]
            # out: [num_col, i, hidden_size]   [2, 1, 24]
            # ht: [n_layers, num_col, hidden_size]   [1, 2, 24]
            # ct: [n_layers, num_col, hidden_size]   [1, 2, 24]
            out, (ht, ct) = self.lstm(xt, (ht, ct))

            # htt: [num_col, n_layers, hidden_size]   [2, 1, 24]
            htt = ht.permute(1, 0, 2)

            # htt: [num_col, -1, hidden_size]   [2, 24]  取[num_col(i), -1(最后), hidden_size]
            htt = htt[:, -1, :]

            if t != num_obs_to_train - 1:
                H[:, t, :] = htt
        # H: [num_col, num_obs_to_train-1, hidden_size]   [2, 4319, 24]
        H = self.relu(H)

        # reshape hidden states H
        # H: [num_col, 1,  num_obs_to_train-1, hidden_size]    [2, 1, 4319, 24]
        H = H.view(-1, 1, num_obs_to_train-1, self.hidden_size)

        # htt: [num_col, -1, hidden_size]   [2, 24]  取[num_col(i), -1(最后), hidden_size]
        # new_ht:[num_col, hidden_size]     [2, 24]
        new_ht = self.attention(H, htt)

        # [num_col, predict_seq_len]   [2, 720]
        ypred = self.linear(new_ht)
        return ypred

class TemporalPatternAttention(nn.Module):

    def __init__(self, filter_size, filter_num, attn_len, attn_size):
        super(TemporalPatternAttention, self).__init__()
        self.filter_size = filter_size  # 1
        self.filter_num = filter_num    #### 32
        self.feat_size = attn_size - self.filter_size + 1  # hidden_size - 1 + 1
        self.conv = nn.Conv2d(1, filter_num, (attn_len, filter_size))   # (1, 32, )
        self.linear1 = nn.Linear(attn_size, filter_num)
        self.linear2 = nn.Linear(attn_size + self.filter_num, attn_size)
        self.relu = nn.ReLU()

    def forward(self, H, ht):
        # H: [num_col, 1(channels),  num_obs_to_train-1, hidden_size(attn_size)]   [2, 1, 4319, 24]
        _, channels, _, attn_size = H.size()

        # ht:取[num_col(i), -1(最后), hidden_size]
        # new_ht: [num_col, 1, hidden_size]   [2, 1, 24]
        new_ht = ht.view(-1, 1, attn_size)

        # [batch_size, 1, filter_num]   [2, 1, 32]  attn_size→filter_num
        w = self.linear1(new_ht)  # batch_size, 1, filter_num

        # H: [num_col, 1(channels),  num_obs_to_train-1, hidden_size(attn_size)]   [2, 1, 4319, 24]
        # [2, 32, 1, 24]  # #####
        conv_vecs = self.conv(H)

        # [num_col, self.feat_size, self.filter_num]  [2, 24, 32]
        conv_vecs = conv_vecs.view(-1, self.feat_size, self.filter_num)

        # [num_col, self.feat_size, self.filter_num], [2, 24, 32]
        conv_vecs = self.relu(conv_vecs)

        # score function
        # [num_col, self.feat_size, self.filter_num]  [2, 24, 32]
        w = w.expand(-1, self.feat_size, self.filter_num)

        # [num_col, self.feat_size]   [2, 24]
        s = torch.mul(conv_vecs, w).sum(dim=2)

        # [num_col, self.feat_size]   [2, 24]
        alpha = torch.sigmoid(s)

        # [num_col, self.feat_size, self.filter_num]  [2, 24, 32]
        new_alpha = alpha.view(-1, self.feat_size, 1).expand(-1, self.feat_size, self.filter_num)

        # [num_col, self.filter_num]  [2, 32]
        v = torch.mul(new_alpha, conv_vecs).sum(dim=1).view(-1, self.filter_num)

        # [num_col, self.feat_size+self.filter_num] [2, 56]
        concat = torch.cat([ht, v], dim=1)

        new_ht = self.linear2(concat)
        return new_ht
