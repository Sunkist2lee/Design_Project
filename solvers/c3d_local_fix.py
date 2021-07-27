#! /usr/bin/env python
import os
import pdb
import time
import yaml
import json
import pickle
import random
import shutil
import argparse
import numpy as np
from collections import defaultdict

# torch
import torch
import torch.nn as nn
import torch.nn.functional as F

# plot
import matplotlib
matplotlib.use('Agg')
from matplotlib import pyplot as plt
import seaborn as sns

from utils import AverageMeter, LearningRate, accuracy, LossWeightDecay
from solvers import BaselineSolver


class Local3dFixSolver(BaselineSolver):

    def build_optimizer(self):
        if self.cfg.optimizer == 'SGD':
            self.optimizer_backbone = self._build_sgd(
                self.model.module.backbone,
            )
            self.optimizer_top = self._build_sgd(
                self.model.module.spatial_pool,
                self.model.module.temporal_pool,
                self.model.module.hpm,
            )
            self.optimizer_local = self._build_sgd(
                self.model.module.local,
            )

        elif self.cfg.optimizer == 'Adam':
            self.optimizer_backbone = self._build_adam(
                self.model.module.backbone,
            )
            self.optimizer_top = self._build_adam(
                self.model.module.spatial_pool,
                self.model.module.temporal_pool,
                self.model.module.hpm,
            )
            self.optimizer_local = self._build_adam(
                self.model.module.local,
            )

        else:
            raise ValueError()
        self.lr_scheduler_backbone = LearningRate(self.optimizer_backbone,
                                                  **self.cfg.lr_decay_backbone)
        self.lr_scheduler_top = LearningRate(self.optimizer_top,
                                             **self.cfg.lr_decay_top)
        self.lr_scheduler_local = LearningRate(self.optimizer_local,
                                             **self.cfg.lr_decay_local)

    def save_checkpoint(self, filename):
        state = {
            'iteration': self.iter,
            'model': self.model.module.state_dict(),
            'optimizer_backbone': self.optimizer_backbone.state_dict(),
            'optimizer_top': self.optimizer_top.state_dict(),
            'optimizer_local': self.optimizer_local.state_dict(),
        }
        torch.save(state, filename)
        self.print_log('Save checkpoint to {}'.format(filename))
        return self.iter

    def load_checkpoint(self, filename, optim=True):
        state = torch.load(filename)
        iter = state['iteration']
        self.model.module.load_state_dict(state['model'])
        if optim:
            self.optimizer_backbone.load_state_dict(state['optimizer_backbone'])
            self.optimizer_top.load_state_dict(state['optimizer_top'])
            self.optimizer_local.load_state_dict(state['optimizer_local'])
            self.print_log('Load weights and optim from {}'.format(filename))
        else:
            self.print_log('Load weights from {}'.format(filename))
        return iter

    def build_loss(self):
        self.criterion_early = self._build_one_loss(self.cfg.early_loss,
                                                    self.cfg.early_loss_args)
        self.early_loss_weight = LossWeightDecay(**self.cfg.early_loss_weight)
        self.local_loss_weight = LossWeightDecay(**self.cfg.local_loss_weight)


    def train(self):
        self.build_data()
        self.build_model()
        self.build_optimizer()
        self.build_loss()
        start_time = time.time()
        self.iter = 0

        # Print out configurations
        self.print_log('{} samples in train set'.format(
            len(self.trainloader.dataset)))
        self.print_log('{} samples in test set'.format(
            len(self.testloader.dataset)))
        if self.cfg.print_model:
            self.print_log('Architecture:\n{}'.format(self.model))
            num_params = sum(p.numel() for p in self.model.parameters() if p.requires_grad)
            self.print_log('Parameters: {}'.format(num_params))
        self.print_log('Configurations:\n{}\n'.format(
            json.dumps(vars(self.cfg), indent=4)))

        # Load from previous checkpoints
        self.load()

        # Test before training
        if self.cfg.test_before_train == True:
            self._test()

        # Meters
        self.best_acc, self.best_iter = [0], -1
        meters = defaultdict(lambda: AverageMeter())

        end = time.time()
        for seq, view, seq_type, label in self.trainloader:
            self.model.train()
            meters['dataTime'].update(time.time() - end)
            end = time.time()

            lr_backbone = self.lr_scheduler_backbone.step(self.iter)
            lr_top = self.lr_scheduler_top.step(self.iter)
            lr_local = self.lr_scheduler_local.step(self.iter)

            lw_early = self.early_loss_weight.step(self.iter)
            lw_local = self.local_loss_weight.step(self.iter)

            self.iter += 1
            seq, label = seq.float().cuda(), label.long().cuda()

            # forward and calculate loss
            feat_global, feat_local = self.model(seq)
            early_loss, loss_num = self.criterion_early(feat_global, label)
            local_loss, local_loss_num = self.criterion_early(feat_local, label)

            loss = lw_early*early_loss + lw_local*local_loss

            # backward
            self.optimizer_backbone.zero_grad()
            self.optimizer_top.zero_grad()
            self.optimizer_local.zero_grad()
            loss.backward()
            self.optimizer_backbone.step()
            self.optimizer_top.step()
            self.optimizer_local.step()

            meters['modelTime'].update(time.time() - end)
            meters['earlyLoss'].update(early_loss)
            meters['localLoss'].update(local_loss)
            meters['lossNum'].update(loss_num)
            meters['localNum'].update(local_loss_num)

            # show log info
            if self.iter % self.cfg.log_interval == 0:
                self.print_log('Iter: {}/{}'.format(self.iter, self.cfg.num_iter) +
                               ' - Data: {:.0f}s'.format(meters['dataTime'].sum) +
                               ' - Model: {:.0f}s'.format(meters['modelTime'].sum) +
                               ' - Backbone: {:.2e}'.format(lr_backbone) +
                               ' - Top: {:.2e}'.format(lr_top) +
                               ' - Local: {:.2e}'.format(lr_local) +
                               ' - Early: {:.2f}'.format(lw_early) +
                               ' - Local: {:.2f}'.format(lw_local) +
                               ' - Num: {:.2e}'.format(meters['lossNum'].avg) +
                               ' - localNum: {:.2e}'.format(meters['localNum'].avg))
                for i in ['earlyLoss', 'localLoss', 'lossNum', 'localNum']:
                    self.writer.add_scalar('train/{}'.format(i), meters[i].avg, self.iter)

                for m in meters.values():
                    m.reset()

                # show distributions of weights and grads
                self.show_info()

            # save checkpoints
            self.save()

            # test
            if self.iter % self.cfg.test_interval == 0:
                acc = self._test()
                self.collect(acc)

            if self.iter == self.cfg.num_iter:
                self.print_log('\nBest Acc: {}'.format(self.best_acc) +
                               '\nIter: {}'.format(self.best_iter) +
                               '\nDir: {}'.format(self.work_dir) +
                               '\nTime: {}'.format(
                                   self._convert_time(time.time() - start_time)))
                return
            end = time.time()


    def _test(self):
        self.model.eval()

        full_feat_list = list()
        local_feat_list = list()
        view_list = list()
        seq_type_list = list()
        label_list = list()

        for i, x in enumerate(self.testloader):
            seq, view, seq_type, label = x
            seq = seq.float().cuda()

            feat_full, feat_local = self.model(seq)
            n = feat_full.size(0)
            full_feat_list.append(feat_full.view(n, -1).data.cpu().numpy())
            local_feat_list.append(feat_local.view(n, -1).data.cpu().numpy())
            view_list += view
            seq_type_list += seq_type
            label_list.append(label.item())

        self.print_log('Test Full')
        acc_full = self._compute_accuracy(full_feat_list, view_list, seq_type_list,
                                          label_list)
        self.print_log('Test Local')
        acc_local = self._compute_accuracy(local_feat_list, view_list, seq_type_list,
                                           label_list)

        if len(acc_full) > 1:
            self.writer.add_scalar('test/fullAccNM', acc_full[0], self.iter)
            self.writer.add_scalar('test/fullAccBG', acc_full[1], self.iter)
            self.writer.add_scalar('test/fullAccCL', acc_full[2], self.iter)
            self.writer.add_scalar('test/localAccNM', acc_local[0], self.iter)
            self.writer.add_scalar('test/localAccBG', acc_local[1], self.iter)
            self.writer.add_scalar('test/localAccCL', acc_local[2], self.iter)
        else:
            self.writer.add_scalar('test/fullAcc', acc_full[0], self.iter)
            self.writer.add_scalar('test/localAcc', acc_local[0], self.iter)
        target_acc = getattr(self.cfg, 'target_acc', 'full')
        accs = {'full': acc_full, 'local': acc_local}
        return accs[target_acc]
