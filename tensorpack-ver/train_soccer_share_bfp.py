#!/usr/bin/env python
# -*- coding: utf-8 -*-

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
from tensorpack.RL import *
import tensorflow as tf

from DQNBFPIModel import Model as DQNModel
import common
from common import play_model, Evaluator, eval_model_multithread
from soccer_env import SoccerPlayer
from augment_expreplay_backward import AugmentExpReplay

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
INIT_EXP = 1.0
STEPS_PER_EPOCH = 10000 // UPDATE_FREQ * 10  # each epoch is 100k played frames
EVAL_EPISODE = 50
LAMB = 1.0
FP_DECAY = 0.1
LR = 1e-3


NUM_ACTIONS = None
METHOD = None
FIELD = None
USE_RNN = False

def get_player(viz=False, train=False):
    pl = SoccerPlayer(image_shape=IMAGE_SIZE[::-1], viz=viz, frame_skip=ACTION_REPEAT, field=FIELD, ai_frame_skip=2)
    if not train:
        # create a new axis to stack history on
        pl = MapPlayerState(pl, lambda im: im[:, :, np.newaxis])
        # in training, history is taken care of in expreplay buffer
        pl = HistoryFramePlayer(pl, FRAME_HISTORY)

        pl = PreventStuckPlayer(pl, 30, 1)
    #pl = LimitLengthPlayer(pl, 30000)
    return pl


class Model(DQNModel):
    def __init__(self):
        super(Model, self).__init__(IMAGE_SIZE, FRAME_HISTORY, METHOD, NUM_ACTIONS, GAMMA, lr=LR, lamb=LAMB, fp_decay=FP_DECAY)

    def _get_DQN_prediction(self, image):
        """ image: [0,255]"""
        image = image / 255.0
        self.batch_size = tf.shape(image)[0]

        if USE_RNN:
            image = tf.transpose(image, perm=[0, 3, 1, 2])
            image = tf.reshape(image, (self.batch_size * self.channel,) + self.image_shape + (1,))

        with tf.variable_scope('network'):
            with argscope(Conv2D, nl=PReLU.symbolic_function, use_bias=True), \
                    argscope(LeakyReLU, alpha=0.01):

                cell = tf.nn.rnn_cell.GRUCell(num_units=128)
                s_l = Conv2D('conv0', image, out_channel=32, kernel_shape=8, stride=4)
                s_l = Conv2D('conv1', s_l, out_channel=64, kernel_shape=4, stride=2)

                p_c = Conv2D('pconv0', s_l, out_channel=64, kernel_shape=3)
                p_l = FullyConnected('pfc0', p_c, 512, nl=LeakyReLU)

                if USE_RNN:
                    p_h = tf.reshape(p_l, [self.batch_size, self.channel, 512])
                    p_h, _ = tf.nn.dynamic_rnn(inputs=p_h,
                             cell=tf.nn.rnn_cell.GRUCell(num_units=256),
                             dtype=tf.float32, scope='prnn')
                else:
                    p_h = FullyConnected('pfc1', p_l, 256, nl=LeakyReLU)

                q_c = Conv2D('qconv0', s_l, out_channel=64, kernel_shape=3)
                q_l = FullyConnected('qfc0', q_c, 512, nl=LeakyReLU)

                if USE_RNN:
                    q_h = tf.reshape(q_l, [self.batch_size, self.channel, 512])
                    q_h, _ = tf.nn.dynamic_rnn(inputs=q_h,
                             cell=tf.nn.rnn_cell.GRUCell(num_units=256),
                             dtype=tf.float32, scope='qrnn')
                else:
                    q_h = FullyConnected('qfc1', q_l, 256, nl=LeakyReLU)


                l = tf.multiply(q_h, p_h, name='mul')
                pi_y = FullyConnected('fcpi0', l, 128, nl=LeakyReLU)
                pi_y = FullyConnected('fcpi2', pi_y, self.num_actions, nl=tf.identity)

                bp_y = FullyConnected('fcbp0', l, 128, nl=LeakyReLU)
                bp_y = FullyConnected('fcbp2', bp_y, self.num_actions, nl=tf.identity)

                fp_y = FullyConnected('fcfp0', l, 128, nl=LeakyReLU)
                fp_y = FullyConnected('fcfp2', fp_y, self.num_actions, nl=tf.identity)
                q_f = FullyConnected('qf', l, 256, nl=LeakyReLU)

        if self.method != 'Dueling':
            Q = FullyConnected('fct', q_f, self.num_actions, nl=tf.identity)
        else:
            # Dueling DQN
            V = FullyConnected('fctV', q_f, 1, nl=tf.identity)
            As = FullyConnected('fctA', q_f, self.num_actions, nl=tf.identity)
            Q = tf.add(As, V - tf.reduce_mean(As, 1, keep_dims=True))

        return tf.identity(Q, name='Qvalue'), tf.identity(pi_y, name='Pivalue'), tf.identity(bp_y, name='Bpvalue'), tf.identity(fp_y, name='Fpvalue')

def get_config():
    M = Model()
    expreplay = AugmentExpReplay(
        predictor_io_names=(['state'], ['Qvalue']),
        player=get_player(train=True),
        state_shape=IMAGE_SIZE,
        batch_size=BATCH_SIZE,
        memory_size=MEMORY_SIZE,
        init_memory_size=INIT_MEMORY_SIZE,
        init_exploration=INIT_EXP,
        update_frequency=UPDATE_FREQ,
        history_len=FRAME_HISTORY
    )

    return TrainConfig(
        dataflow=expreplay,
        callbacks=[
            ModelSaver(),
            PeriodicTrigger(
                RunOp(DQNModel.update_target_param, verbose=True),
                every_k_steps=10000 // UPDATE_FREQ),    # update target network every 10k steps
            expreplay,
            ScheduledHyperParamSetter('learning_rate',
                                      #[(20, 4e-4), (40, 2e-4)]),
                                      [(40, 4e-4), (80, 2e-4)]),
            ScheduledHyperParamSetter(
                ObjAttrParam(expreplay, 'exploration'),
                #[(0, 1), (10, 0.1), (320, 0.01)],   # 1->0.1 in the first million steps
                [(0, 1), (10, 0.1), (100, 0.01)],   # 1->0.1 in the first million steps
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
    parser.add_argument('--task', help='task to perform',
                        choices=['play', 'eval', 'train'], default='train')
    parser.add_argument('--algo', help='algorithm',
                        choices=['DQN', 'Double', 'Dueling'], default='DQN')
    parser.add_argument('--skip', help='act repeat', type=int, required=True)
    parser.add_argument('--field', help='field type', type=str, choices=['small', 'large'], required=True)
    parser.add_argument('--hist_len', help='hist len', type=int, required=True)
    parser.add_argument('--batch_size', help='batch size', type=int, required=True)
    parser.add_argument('--lamb', dest='lamb', type=float, default=1.0)
    parser.add_argument('--fp_decay', dest='fp_decay', type=float, default=0.1)
    parser.add_argument('--rnn', dest='rnn', action='store_true')

    args = parser.parse_args()

    if args.gpu:
        os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu
    METHOD = args.algo

    ACTION_REPEAT = args.skip
    FIELD = args.field
    FRAME_HISTORY = args.hist_len
    BATCH_SIZE = args.batch_size
    LAMB = args.lamb
    USE_RNN = args.rnn
    FP_DECAY = args.fp_decay

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
            os.path.join('train_log', 'DQNBFPI-SHARE-field-{}-skip-{}-hist-{}-batch-{}-{}-{}_fast_decay'.format(
                args.field, args.skip, args.hist_len, args.batch_size, os.path.basename('soccer').split('.')[0], LAMB)))
        config = get_config()
        if args.load:
            config.session_init = SaverRestore(args.load)
        QueueInputTrainer(config).train()