import sys
from pathlib import Path
from omegaconf import OmegaConf
import numpy as np
import torch
from tqdm import tqdm

from config import Config
from lexa import LEXA
from envs.franka_kitchen import FrankaKichenEnv
from replay_buffer import ReplayBuffer
from utils import fix_seed, preprocess_obs


base_path = Path(__file__).parents[1] / 'outputs'


def main(cfg):
    cfg = Config(**cfg)
    fix_seed(cfg.seed)
    
    env = FrankaKichenEnv(cfg.env.img_size, cfg.env.action_repeat, cfg.env.time_limit, cfg.seed)
    eval_env = FrankaKichenEnv(cfg.env.img_size, cfg.env.action_repeat, cfg.env.time_limit, cfg.seed)
    
    lexa = LEXA(cfg, env)
    replay_buffer = ReplayBuffer(cfg.data.buffer_size,
                                 (3, cfg.env.img_size, cfg.env.img_size),
                                 env.action_space.shape[0])
    
    obs = env.reset()
    
    # seed steps
    for step in range(cfg.learning.seed_steps):
        action = env.action_space.sample()
        next_obs, reward, done, info = env.step(action)
        replay_buffer.push(preprocess_obs(obs), action, done)
        obs = next_obs
        if done:
            obs = env.reset()
    
    # learning steps
    obs = env.reset()
    goal = None
    episodes = 0
    best_score = -1
    for step in tqdm(range(cfg.learning.num_steps)):
        with torch.no_grad():
            if episodes % cfg.learning.expl_episode_freq == 0:
                mode = 'explorer'
            else:
                mode = 'achiever'
            
            action = lexa.agent(preprocess_obs(obs), mode, goal)
            next_obs, reward, done, info = env.step(action)
            replay_buffer.push(preprocess_obs(obs), action, done)
            obs = next_obs
        
        if (step + 1) % cfg.learning.update_freq:
            observations, actions, done_flags = replay_buffer.sample(cfg.data.batch_size, cfg.data.seq_length)
            metrics = lexa.train(observations, actions)
        
        if (step + 1) % cfg.model.explorer.slow_critic_update:
            lexa.explorer.update_critic()
        if (step + 1) % cfg.model.achiever.slow_critic_update:
            lexa.achiever.update_critic()
        
        if done:
            print(f'steps: {step + 1}, episode: {episodes}, {metrics}')
            lexa.save(base_path / cfg.wandb.name / f'{step + 1}')
            episodes += 1
            obs = env.reset()
            lexa.agent.reset()
            goal, _, _ = replay_buffer.sample(1, 1)
            goal = goal.squeeze(1)
            if episodes % cfg.learning.eval_episode_freq:
                with torch.no_grad():
                    success = 0
                    for goal_idx in eval_env.goals:
                        eval_obs = eval_env.reset()
                        eval_env.set_goal_idx(goal_idx)
                        goal_obs = eval_env.get_goal_obs()
                        eval_done = False
                        while not eval_done:
                            eval_action = lexa.agent(preprocess_obs(eval_obs), 'achiever', preprocess_obs(goal_obs), train=False)
                            eval_obs, eval_reward, eval_done, eval_info = eval_env.step(eval_action)
                        if eval_env.compute_success():
                            success += 1
                    score = success / len(eval_env.goals)
                print(f'steps: {step + 1}, episode: {episodes}, eval_score: {score}')
                if score > best_score:
                    best_score = score
                    lexa.save(base_path / cfg.wandb.name / 'best')
                lexa.agent.reset()


if __name__ == '__main__':
    args = sys.argv
    cfg = OmegaConf.load(args[1])
    main(cfg)
