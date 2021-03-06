#!/usr/bin/env python
# -*- coding: utf-8 -*-
# File: train_soccer.py
# Author: Zhangwei hong <williamd4112@hotmail.com>

import numpy as np

import os
import sys
import re
import time
import random
import argparse
import subprocess
import multiprocessing
import threading
from collections import deque

from tensorpack import *
from tensorpack.utils.concurrency import *
from tensorpack.tfutils import symbolic_functions as symbf
from tensorpack.RL import *
import tensorflow as tf

from DPIQNModel import Model as DQNModel
import common
from common import play_model, Evaluator, eval_model_multithread
from soccer_env import SoccerPlayer
from augment_expreplay import AugmentExpReplay
from tensorpack.tfutils import symbolic_functions as symbf

BATCH_SIZE = None
IMAGE_SIZE = (84, 84)
FRAME_HISTORY = None
ACTION_REPEAT = None   # aka FRAME_SKIP
UPDATE_FREQ = 4

GAMMA = 0.99

MEMORY_SIZE = 1e6
# will consume at least 1e6 * 84 * 84 bytes == 6.6G memory.
INIT_MEMORY_SIZE = 5e4
STEPS_PER_EPOCH = 1000 // UPDATE_FREQ * 10  # each epoch is 100k played frames
EVAL_EPISODE = 50

NUM_ACTIONS = None
METHOD = None
FIELD = None
LR = None
AI_SKIP = None
USE_RNN = False
RNN_CELL = 'lstm'
FC_HIDDEN = 512
RNN_HIDDEN = 512
RNN_STEP = 1
UPDATE_TARGET_STEP = 10000
MULTI_TASK = False
LR_SCHED = None
EPS_SCHED = None
MODE = None
MULTI_TASK_MODE = None
REG = None
TASK = None
PI_COEF = 1.0

def get_player(viz=False, train=False):
    pl = SoccerPlayer(image_shape=IMAGE_SIZE[::-1], viz=viz, frame_skip=ACTION_REPEAT, field=FIELD, ai_frame_skip=AI_SKIP, team_size=2 if MULTI_TASK else 1, mode=MODE)
    if not train:
        # create a new axis to stack history on
        pl = MapPlayerState(pl, lambda im: im[:, :, np.newaxis])
        # in training, history is taken care of in expreplay buffer
        pl = HistoryFramePlayer(pl, FRAME_HISTORY)

        pl = PreventStuckPlayer(pl, 30, 1)
    #pl = LimitLengthPlayer(pl, 30000)
    return pl

def get_rnn_cell():
    if RNN_CELL == 'gru':
        return tf.nn.rnn_cell.GRUCell(num_units=RNN_HIDDEN, activation=tf.nn.relu)
    elif RNN_CELL == 'lstm':
        return tf.nn.rnn_cell.LSTMCell(num_units=RNN_HIDDEN, state_is_tuple=True, activation=tf.nn.relu)
    else:
        assert 0

class Model(DQNModel):
    def __init__(self):
        super(Model, self).__init__(IMAGE_SIZE, FRAME_HISTORY, METHOD,
            NUM_ACTIONS, GAMMA, LR, PI_COEF, RNN_HIDDEN, RNN_STEP, MULTI_TASK, 3 if MULTI_TASK else 1, REG, MULTI_TASK_MODE)

    def get_rnn_init_state(self, cell, name):
        return cell.zero_state(self.batch_size, tf.float32)

    def _get_DQN_prediction(self, image):
        """ image: [0,255]"""
        image = image / 255.0

        if USE_RNN:
            self.batch_size = tf.shape(image)[0]
            image = tf.transpose(image, perm=[0, 3, 1, 2])
            image = tf.reshape(image, (self.batch_size * self.channel,) + self.image_shape + (1,))

        with tf.variable_scope('q'):
            with argscope(Conv2D, nl=PReLU.symbolic_function, use_bias=True, padding='SAME'), \
                    argscope(LeakyReLU, alpha=0.01):
                h = (LinearWrap(image)
                     .Conv2D('conv0', out_channel=32, kernel_shape=8, stride=4)
                     .Conv2D('conv1', out_channel=64, kernel_shape=4, stride=2)
                     .Conv2D('conv2', out_channel=64, kernel_shape=3)())

                q_l = FullyConnected('fc0-q', h, FC_HIDDEN, nl=LeakyReLU)
                pi_l = FullyConnected('fc0-pi', h, FC_HIDDEN, nl=LeakyReLU)

                if USE_RNN:
                    # q
                    q_l = tf.reshape(q_l, [self.batch_size, self.channel, FC_HIDDEN])
                    q_cell = get_rnn_cell()
                    q_l, q_rnn_state_out = tf.nn.dynamic_rnn(inputs=q_l,
                                cell=q_cell,
                                initial_state=self.get_rnn_init_state(q_cell, 'q'),
                                dtype=tf.float32, scope='rnn-q')
                    q_l = q_l[:, -RNN_STEP:, :]
                    q_l = tf.reshape(q_l, (self.batch_size * RNN_STEP, RNN_HIDDEN))

                    # pi
                    pi_l = tf.reshape(pi_l, [self.batch_size, self.channel, FC_HIDDEN])
                    pi_cell = get_rnn_cell()
                    pi_l, pi_rnn_state_out = tf.nn.dynamic_rnn(inputs=pi_l,
                                cell=pi_cell,
                                initial_state=self.get_rnn_init_state(pi_cell, 'pi'),
                                dtype=tf.float32, scope='rnn-pi')
                    pi_l = pi_l[:, -RNN_STEP:, :]
                    pi_l = tf.reshape(pi_l, (self.batch_size * RNN_STEP, RNN_HIDDEN))

        pi_ys = []
        for i in range(self.num_agents):
            pi_ys.append(FullyConnected('fct-%d' % i, pi_l, self.num_actions, nl=tf.identity))

        l = tf.multiply(q_l, pi_l)

        if self.method != 'Dueling':
            Q = FullyConnected('fct', l, self.num_actions, nl=tf.identity)
        else:
            # Dueling DQN
            V = FullyConnected('fctV', l, 1, nl=tf.identity)
            As = FullyConnected('fctA', l, self.num_actions, nl=tf.identity)
            Q = tf.add(As, V - tf.reduce_mean(As, 1, keep_dims=True))

        pi_values = [ tf.identity(pi_ys[i], name='Pivalue-%d' % i) for i in range(self.num_agents) ]

        return tf.identity(Q, name='Qvalue'), pi_values, None, None

def get_config():
    if TASK == 'play':
        if MULTI_TASK:
            predictor_io_names=(['state'], ['Qvalue', 'Pivalue-0', 'Pivalue-1', 'Pivalue-2'])
        else:
            predictor_io_names=(['state'], ['Qvalue', 'Pivalue-0'])
    else:
        predictor_io_names=(['state'], ['Qvalue'])

    M = Model()
    expreplay = AugmentExpReplay(
        predictor_io_names=predictor_io_names,
        player=get_player(train=True),
        state_shape=IMAGE_SIZE,
        batch_size=BATCH_SIZE,
        memory_size=MEMORY_SIZE,
        init_memory_size=INIT_MEMORY_SIZE,
        init_exploration=1.0,
        update_frequency=UPDATE_FREQ,
        history_len=FRAME_HISTORY,
        h_size=RNN_HIDDEN,
        num_agents=(3 if MULTI_TASK else 1)
    )

    lr_schedule = []
    for p in LR_SCHED.split(','):
        ep, lr = p.split(':')
        lr_schedule.append((int(ep), float(lr)))

    eps_schedule = []
    for p in EPS_SCHED.split(','):
        ep, eps = p.split(':')
        eps_schedule.append((int(ep), float(eps)))

    return TrainConfig(
        dataflow=expreplay,
        callbacks=[
            ModelSaver(),
            PeriodicTrigger(
                RunOp(DQNModel.update_target_param, verbose=True),
                every_k_steps=UPDATE_TARGET_STEP // UPDATE_FREQ),    # update target network every 10k steps
            expreplay,
            ScheduledHyperParamSetter('learning_rate',
                                      lr_schedule),
            ScheduledHyperParamSetter(
                ObjAttrParam(expreplay, 'exploration'),
                eps_schedule,   # 1->0.1 in the first million steps
                interp='linear'),
            HumanHyperParamSetter('learning_rate'),
        ],
        model=M,
        steps_per_epoch=STEPS_PER_EPOCH,
        max_epoch=10000,
        # run the simulator on a separate GPU if available
        predict_tower=[1] if get_nr_gpu() > 1 else [0],
    )


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--gpu', help='comma separated list of GPU(s) to use.')
    parser.add_argument('--load', help='load model')
    parser.add_argument('--log', help='train log dir', default='train_log')
    parser.add_argument('--task', help='task to perform',
                        choices=['play', 'eval', 'train'], default='train')
    parser.add_argument('--algo', help='algorithm for computing Q-value',
                        choices=['DQN', 'Double', 'Dueling'], default='DQN')
    parser.add_argument('--mode', help='specify ai mode in env', type=str, default=None)
    parser.add_argument('--mt_mode', help='multi-task setting',
                        choices=['coop-only', 'opponent-only', 'all'], default='all')
    parser.add_argument('--mt', help='use 2v2 env', action='store_true', default=False)
    parser.add_argument('--skip', help='act repeat', type=int, default=2)
    parser.add_argument('--hist_len', help='hist len', type=int, default=12)
    parser.add_argument('--batch_size', help='batch size', type=int, default=32)
    parser.add_argument('--lr', help='init lr value', type=float, default=1e-3)
    parser.add_argument('--rnn', help='use rnn (DRPIQN)', type=str, default=False)
    parser.add_argument('--lr_sched', help='lr schedule', type=str, default='600:4e-4,1000:2e-4')
    parser.add_argument('--eps_sched', help='eps decay schedule', type=str, default='100:0.1,3200:0.01')
    parser.add_argument('--reg', help='reg', action='store_true', default=False)
    args = parser.parse_args()

    if args.gpu:
        os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu
    METHOD = args.algo

    ACTION_REPEAT = AI_SKIP = args.skip
    FRAME_HISTORY = args.hist_len
    BATCH_SIZE = args.batch_size
    LR = args.lr
    MULTI_TASK = args.mt
    MULTI_TASK_MODE = args.mt_mode
    USE_RNN = args.rnn
    LR_SCHED = args.lr_sched
    EPS_SCHED = args.eps_sched
    MODE = args.mode
    REG = args.reg
    train_logdir = args.log
    TASK = args.task
    FIELD = 'large' if args.mt else 'small'

    if MULTI_TASK:
        scenario = 'MT-%s' % MULTI_TASK_MODE
    else:
        scenario = 'ST'

    if USE_RNN:
        MODEL_NAME = '%s-%s-RPI' % (scenario, args.algo)
    else:
        MODEL_NAME = '%s-%s-PI' % (scenario, args.algo)

    # set num_actions
    NUM_ACTIONS = SoccerPlayer().get_action_space().num_actions()

    if args.task != 'train':
        assert args.load is not None
        cfg = PredictConfig(
            model=Model(),
            session_init=get_model_loader(args.load),
            input_names=['state'],
            output_names=['Qvalue'])
        if args.task == 'play':
            play_model(cfg, get_player(viz=1))
        elif args.task == 'eval':
            eval_model_multithread(cfg, EVAL_EPISODE, get_player)
    else:
        logger.set_logger_dir(
            os.path.join(train_logdir, '{}-skip-{}-hist-{}-batch-{}-lr-{}-{}-eps-{}-reg-{}-{}'.format(
                MODEL_NAME, args.skip, args.hist_len, args.batch_size, args.lr,
                args.lr_sched, args.eps_sched, REG, os.path.basename('soccer').split('.')[0])))
        config = get_config()
        if args.load:
            config.session_init = SaverRestore(args.load)
        QueueInputTrainer(config).train()
