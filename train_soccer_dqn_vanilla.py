import gym

from baselines import deepq
from baselines.common.atari_wrappers_deprecated import EpisodicLifeEnv, NoopResetEnv, MaxAndSkipEnv, FrameStack, ClippedRewardsWrapper, ScaledFloatFrame

from soccer_env import SoccerEnv, ProcessSoccerFrame84

def wrap_dqn_for_soccer(env):
    """Apply a common set of wrappers for Atari games."""
    env = NoopResetEnv(env, noop_max=30)
    env = MaxAndSkipEnv(env, skip=4)
    env = ProcessSoccerFrame84(env)
    env = FrameStack(env, 4)
    env = ClippedRewardsWrapper(env)
    return env


def main():
    env = SoccerEnv(frameskip=1)
    env = ScaledFloatFrame(wrap_dqn_for_soccer(env))
    model = deepq.models.cnn_to_mlp(
        convs=[(32, 8, 4), (64, 4, 2), (64, 3, 1)],
        hiddens=[256],
        dueling=False
    )
    act = deepq.learn(
        env,
        q_func=model,
        lr=1e-4,
        max_timesteps=200000,
        buffer_size=10000,
        exploration_fraction=0.1,
        exploration_final_eps=0.01,
        train_freq=4,
        learning_starts=10000,
        target_network_update_freq=1000,
        gamma=0.99,
        prioritized_replay=False
    )
    act.save("soccer_model.pkl")
    env.close()


if __name__ == '__main__':
    main()