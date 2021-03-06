import os
from collections import deque

import cv2
import gym
import numpy as np
from gym import spaces

os.environ.setdefault('PATH', '')
cv2.ocl.setUseOpenCL(False)


class NoopResetEnv(gym.Wrapper):
    def __init__(self, env, noop_max=30):
        """Sample initial states by taking random number of no-ops on reset.
        No-op is assumed to be action 0.
        """
        gym.Wrapper.__init__(self, env)
        self.noop_max = noop_max
        self.override_num_noops = None
        self.noop_action = 0
        assert env.unwrapped.get_action_meanings()[0] == 'NOOP'

    def reset(self, **kwargs):
        """ Do no-op action for a number of steps in [1, noop_max]."""
        self.env.reset(**kwargs)
        if self.override_num_noops is not None:
            noops = self.override_num_noops
        else:
            noops = self.unwrapped.np_random.randint(1, self.noop_max + 1)  # pylint: disable=E1101
        assert noops > 0
        obs = None
        for _ in range(noops):
            obs, _, done, _ = self.env.step(self.noop_action)
            if done:
                obs = self.env.reset(**kwargs)
        return obs

    def step(self, ac):
        return self.env.step(ac)


class FireResetEnv(gym.Wrapper):
    def __init__(self, env):
        """Take action on reset for environments that are fixed until firing."""
        gym.Wrapper.__init__(self, env)
        assert env.unwrapped.get_action_meanings()[1] == 'FIRE'
        assert len(env.unwrapped.get_action_meanings()) >= 3

    def reset(self, **kwargs):
        self.env.reset(**kwargs)
        obs, _, done, _ = self.env.step(1)
        if done:
            self.env.reset(**kwargs)
        obs, _, done, _ = self.env.step(2)
        if done:
            self.env.reset(**kwargs)
        return obs

    def step(self, ac):
        return self.env.step(ac)


class EpisodicLifeEnv(gym.Wrapper):
    def __init__(self, env):
        """Make end-of-life == end-of-episode, but only reset on true game over.
        Done by DeepMind for the DQN and co. since it helps value estimation.
        """
        gym.Wrapper.__init__(self, env)
        self.lives = 0
        self.was_real_done = True

        self.trajectory_length = 0
        self.trajectory_reward = 0

    def step(self, action):

        obs, reward, done, info = self.env.step(action)
        # print(f"info: {info}")
        self.was_real_done = done

        # Log trajectory length and trajectory reward.
        self.trajectory_length += 1
        self.trajectory_reward += reward
        if done:
            info["episode"] = {"r": self.trajectory_reward, "l": self.trajectory_length}
            self.trajectory_reward = 0
            self.trajectory_length = 0

        # check current lives, make loss of life terminal,
        # then update lives to handle bonus lives
        lives = self.env.unwrapped.ale.lives()
        if lives < self.lives and lives > 0:
            # for Qbert sometimes we stay in lives == 0 condition for a few frames
            # so it's important to keep lives > 0, so that we only reset once
            # the environment advertises done.
            done = True
        self.lives = lives

        # if self.was_real_done is True:
        #     print("real done in self mode")
        #     obs = self.env.reset()
        #     self.lives = self.env.unwrapped.ale.lives()
        # elif done is True:
        #     print("wei done in self mode")
        #     obs, _, _, _ = self.env.step(0)
        #     print(np.array(obs)[32, 32, :])
        #     self.lives = self.env.unwrapped.ale.lives()

        return obs, reward, done, info

    def reset(self, **kwargs):
        """Reset only when lives are exhausted.
        This way all states are still reachable even though lives are episodic,
        and the learner need not know about any of this behind-the-scenes.
        """
        if self.was_real_done:
            obs = self.env.reset(**kwargs)
            # print("real done <<<<")
        else:
            # no-op step to advance from terminal/lost life state
            obs, _, _, _ = self.env.step(0)
            # print("wei done <<<<")
            # print(np.array(obs)[32, 32, :])
        self.lives = self.env.unwrapped.ale.lives()
        return obs


class MaxAndSkipEnv(gym.Wrapper):
    def __init__(self, env, skip=4):
        """Return only every `skip`-th frame"""
        gym.Wrapper.__init__(self, env)
        # most recent raw observations (for max pooling across time steps)
        self._obs_buffer = np.zeros((2,) + env.observation_space.shape, dtype=np.uint8)
        self._skip = skip

    def step(self, action):
        """Repeat action, sum reward, and max over last observations."""
        total_reward = 0.0
        done = None
        for i in range(self._skip):
            obs, reward, done, info = self.env.step(action)
            if i == self._skip - 2:
                self._obs_buffer[0] = obs
            if i == self._skip - 1:
                self._obs_buffer[1] = obs
            total_reward += reward
            if done:
                break
        # Note that the observation on the done=True frame
        # doesn't matter
        max_frame = self._obs_buffer.max(axis=0)

        info["real_reward"] = total_reward
        info["real_done"] = done

        # print("max_frame:", max_frame.shape)

        return max_frame, total_reward, done, info

    def reset(self, **kwargs):
        return self.env.reset(**kwargs)


class ClipRewardEnv(gym.RewardWrapper):
    def __init__(self, env):
        gym.RewardWrapper.__init__(self, env)

    def reward(self, reward):
        """Bin reward to {+1, 0, -1} by its sign."""
        return np.sign(reward)


class WarpFrame(gym.ObservationWrapper):
    def __init__(self, env, width=84, height=84, grayscale=True):
        """Warp frames to 84x84 as done in the Nature paper and later work."""
        gym.ObservationWrapper.__init__(self, env)
        self.width = width
        self.height = height
        self.grayscale = grayscale
        if self.grayscale:
            self.observation_space = spaces.Box(low=0, high=255,
                                                shape=(self.height, self.width, 1), dtype=np.uint8)
        else:
            self.observation_space = spaces.Box(low=0, high=255,
                                                shape=(self.height, self.width, 3), dtype=np.uint8)

    def observation(self, frame):
        if self.grayscale:
            frame = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
        frame = cv2.resize(frame, (self.width, self.height), interpolation=cv2.INTER_AREA)
        if self.grayscale:
            frame = np.expand_dims(frame, -1)
        return frame


class FrameStack(gym.Wrapper):
    def __init__(self, env, k):
        """Stack k last frames.

        Returns lazy array, which is much more memory efficient.

        See Also
        --------
        baselines.common.atari_wrappers.LazyFrames
        """
        gym.Wrapper.__init__(self, env)
        self.k = k
        self.frames = deque([], maxlen=k)
        shp = env.observation_space.shape
        self.observation_space = spaces.Box(low=0, high=255, shape=(shp[:-1] + (shp[-1] * k,)), dtype=env.observation_space.dtype)

        # self._dim_action = self.env.action_space.n
        # self._dim_observation = self.observation_space.shape

    def reset(self):
        ob = self.env.reset()
        for _ in range(self.k):
            self.frames.append(ob)
        return self._get_ob()

    def step(self, action):
        ob, reward, done, info = self.env.step(action)
        self.frames.append(ob)
        return self._get_ob(), reward, done, info

    def _get_ob(self):
        assert len(self.frames) == self.k
        return LazyFrames(list(self.frames))


class NeverStop(gym.Wrapper):
    def __init__(self, env):
        gym.Wrapper.__init__(self, env)

        self._dim_action = self.env.action_space.n
        self._dim_observation = self.env.observation_space.shape

    def reset(self):
        ob = self.env.reset()
        return np.asarray(ob, dtype=np.uint8)

    def step(self, action):
        ob, rew, done, info = self.env.step(action)
        if done is True:
            ob = self.env.reset()
        return np.asarray(ob, dtype=np.uint8), rew, done, info

    @property
    def dim_action(self):
        return self._dim_action

    @property
    def dim_observation(self):
        return self._dim_observation

    def sample_action(self):
        return self.env.action_space.sample()


class RamStack(gym.Wrapper):
    def __init__(self, env):
        gym.Wrapper.__init__(self, env)

        self._dim_act = self.env.dim_action
        self._dim_obs = self.env.dim_observation

    def reset(self):
        ob = self.env.reset()
        print("ob shape:", ob.shape, "dtype:", ob.dtype, "type:", type(ob))
        input()
        return ob.reshape(4, 128).swapaxes(0, 1)

    def step(self, action):
        ob, rew, done, info = self.env.step(action)
        return ob.reshape(4, 128).swapaxes(0, 1), rew, done, info

    def sample_action(self):
        return self.env.sample_action()

    @property
    def dim_action(self):
        return self._dim_act

    @property
    def dim_observation(self):
        return (128, 4)


class RamStack2(gym.Wrapper):
    def __init__(self, env):
        gym.Wrapper.__init__(self, env)

        self._dim_act = self.env.dim_action
        self._dim_obs = self.env.dim_observation

    def reset(self):
        ob = self.env.reset()
        return ob

    def step(self, action):
        ob, rew, done, info = self.env.step(action)
        return ob, rew, done, info

    def sample_action(self):
        return self.env.sample_action()

    @property
    def dim_action(self):
        return self._dim_act

    @property
    def dim_observation(self):
        return (512,)


class ScaledFloatFrame(gym.ObservationWrapper):
    def __init__(self, env):
        gym.ObservationWrapper.__init__(self, env)
        self.observation_space = gym.spaces.Box(low=0, high=1, shape=env.observation_space.shape, dtype=np.float32)

    def observation(self, observation):
        # careful! This undoes the memory optimization, use
        # with smaller replay buffers only.
        return np.array(observation).astype(np.float32) / 255.0


class LazyFrames(object):
    def __init__(self, frames):
        """This object ensures that common frames between the observations are only stored once.
        It exists purely to optimize memory usage which can be huge for DQN's 1M frames replay
        buffers.

        This object should only be converted to numpy array before being passed to the model.

        You'd not believe how complex the previous solution was."""
        self._frames = frames
        self._out = None

    def _force(self):
        if self._out is None:
            self._out = np.concatenate(self._frames, axis=-1)
            self._frames = None
        return self._out

    def __array__(self, dtype=None):
        out = self._force()
        if dtype is not None:
            out = out.astype(dtype)
        return out

    def __len__(self):
        return len(self._force())

    def __getitem__(self, i):
        return self._force()[i]


def old_make_atari(env_id, timelimit=True):
    # XXX(john): remove timelimit argument after gym is upgraded to allow double wrapping
    env = gym.make(env_id)
    if not timelimit:
        env = env.env
    assert 'NoFrameskip' in env.spec.id
    env = NoopResetEnv(env, noop_max=30)
    env = MaxAndSkipEnv(env, skip=4)
    return env


def wrap_deepmind(env, episode_life=True, clip_rewards=True, frame_stack=False, scale=False, warp=True):
    """Configure environment for DeepMind-style Atari.
    """
    if episode_life:
        env = EpisodicLifeEnv(env)
    if 'FIRE' in env.unwrapped.get_action_meanings():
        env = FireResetEnv(env)
    if warp:
        env = WarpFrame(env)
    if scale:
        env = ScaledFloatFrame(env)
    if clip_rewards:
        env = ClipRewardEnv(env)
    if frame_stack:
        env = FrameStack(env, 4)
    return env


def make_atari(env_name):
    assert "NoFrameskip" in env_name and "ramNoFrameskip" not in env_name
    env = old_make_atari(env_name)
    env = wrap_deepmind(env, episode_life=True, clip_rewards=True, frame_stack=True, scale=False)
    # env = NeverStop(env)
    return env


def make_ram_atari(env_name):
    assert "ramNoFrameskip" in env_name

    env = old_make_atari(env_name)
    env = wrap_deepmind(env, episode_life=True, clip_rewards=True, frame_stack=True, scale=False, warp=False)
    # env = NeverStop(env)
    env = RamStack(env)
    return env


def make_ram_atari2(env_name):
    assert "ramNoFrameskip" in env_name

    env = old_make_atari(env_name)
    env = wrap_deepmind(env, episode_life=True, clip_rewards=True, frame_stack=True, scale=False, warp=False)
    env = NeverStop(env)
    env = RamStack2(env)
    return env


if __name__ == "__main__":
    env = make_atari("AlienNoFrameskip-v4")
    env.seed(1)
    s = env.reset()

    all_r = []
    for i in range(10000):
        s, r, d, info = env.step(env.action_space.sample())

        all_r.append(r)
        if d is True:
            print(f"r max: {np.max(all_r)} min: {np.min(all_r)}")

            if "episode" in info:
                print("info:", info["episode"]["r"], info["episode"]["l"])
            input()
