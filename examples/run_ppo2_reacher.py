import gym
from tqdm import tqdm
import numpy as np
import tensorflow as tf
from tensorboardX import SummaryWriter
from collections import deque
import os

from rlpack.common import NonBlockMemory
from rlpack.environment import MujocoWrapper
from rlpack.algos import ContinuousPPO


class Config(object):
    def __init__(self):
        self.seed = 1
        self.save_path = "./log/ppo_reacher"
        self.save_model_freq = 0.001
        self.log_freq = 10

        # 环境
        self.n_stack = 4
        self.dim_observation = None
        self.dim_action = None   # gym中不同环境的action数目不同。

        # 训练长度
        self.n_env = 8
        self.trajectory_length = 128
        self.n_trajectory = 10000   # for each env
        self.batch_size = 64
        self.warm_start_length = 1
        self.memory_size = 1000

        # 训练参数
        self.training_epochs = 100
        self.discount = 0.99
        self.gae = 0.95
        self.lr_schedule = lambda x: (1-x) * 2.5e-4
        self.clip_schedule = lambda x: (1-x) * 0.1
        self.vf_coef = 1.0
        self.entropy_coef = 0.01
        self.max_grad_norm = 0.5
        self.lr = 3e-4


def process_env(env):
    config = Config()
    config.dim_observation = env.dim_observation
    config.dim_action = env.dim_action[0]

    print(f"dim_action: {env.dim_action}")
    return config


# class Agent(PPO):
#     def __init__(self, config):
#         super().__init__(config)
#
#     def build_network(self):
#         self.observation = tf.placeholder(tf.float32, [None, *self.dim_observation], name="observation")
#         x = tf.layers.dense(self.observation, 256, activation=tf.nn.relu)
#         x = tf.contrib.layers.flatten(x)  # pylint: disable=E1101
#         x = tf.layers.dense(x, 512, activation=tf.nn.relu)
#         self.logit_action_probability = tf.layers.dense(
#             x, self.n_action, activation=None, kernel_initializer=tf.truncated_normal_initializer(0.0, 0.01))
#         self.state_value = tf.squeeze(tf.layers.dense(
#             x, 1, activation=None, kernel_initializer=tf.truncated_normal_initializer()))


def safemean(x):
    return np.nan if len(x) == 0 else np.mean(x)


def learn(env, agent, config):

    memory = NonBlockMemory(config.n_env)
    epinfobuf = deque(maxlen=100)
    summary_writer = SummaryWriter(os.path.join(config.save_path, "summary"))

    # 热启动，随机收集数据。
    tags, obs = env.reset()
    memory.store_tag_s(tags, obs)
    print(f"observation: max={np.max(obs)} min={np.min(obs)}")
    for i in tqdm(range(config.warm_start_length)):
        actions = agent.get_action(obs)
        next_tags, next_obs, rewards, dones, infos = env.step(actions)

        memory.store_tag_a(tags, actions)
        memory.store_tag_rds(next_tags, rewards, dones, next_obs)
        obs = next_obs
        tags = next_tags

    print("Finish warm start.")
    print("Start training.")
    for i in tqdm(range(config.n_trajectory)):
        epinfos = []
        for _ in range(config.trajectory_length):
            actions = agent.get_action(obs)
            next_tags, next_obs, rewards, dones, infos = env.step(actions)

            for info in infos:
                maybeepinfo = info.get('episode')
                if maybeepinfo:
                    epinfos.append(maybeepinfo)

            memory.store_tag_a(tags, actions)
            memory.store_tag_rds(next_tags, rewards, dones, next_obs)
            obs = next_obs
            tags = next_tags

        update_ratio = i/config.n_trajectory
        data_batch = memory.get_last_n_step(config.trajectory_length)
        agent.update(data_batch, update_ratio)

        epinfobuf.extend(epinfos)
        summary_writer.add_scalar("eprewmean", safemean([epinfo["r"] for epinfo in epinfobuf]), global_step=i)
        summary_writer.add_scalar("eplenmean", safemean([epinfo['l'] for epinfo in epinfobuf]), global_step=i)

        if i > 0 and i % config.log_freq == 0:
            rewmean = safemean([epinfo["r"] for epinfo in epinfobuf])
            lenmean = safemean([epinfo['l'] for epinfo in epinfobuf])
            print(f"eprewmean: {rewmean}  eplenmean: {lenmean}")


if __name__ == "__main__":
    env = MujocoWrapper("Reacher-v2", 8)
    config = process_env(env)
    agent = ContinuousPPO(config)
    learn(env, agent, config)