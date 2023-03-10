# https://gymnasium.farama.org/environments/classic_control/cart_pole/
import time
import os
os.environ['KMP_DUPLICATE_LIB_OK'] = 'True'

import gymnasium as gym
import numpy as np
import torch
import torch.optim as optim
import torch.nn.functional as F
from torch.distributions import Normal
import wandb
from datetime import datetime
from shutil import copyfile

from c_actor_and_critic import MODEL_DIR, Actor, Critic, Transition, Buffer


class A2C:
    def __init__(self, env, test_env, config, use_wandb):
        self.env = env
        self.test_env = test_env
        self.use_wandb = use_wandb

        self.env_name = config["env_name"]

        self.current_time = datetime.now().astimezone().strftime('%Y-%m-%d_%H-%M-%S')

        if self.use_wandb:
            self.wandb = wandb.init(
                project="A2C_{0}".format(self.env_name),
                name=self.current_time,
                config=config
            )

        self.max_num_episodes = config["max_num_episodes"]
        self.batch_size = config["batch_size"]
        self.learning_rate = config["learning_rate"]
        self.gamma = config["gamma"]
        self.entropy_beta = config["entropy_beta"]
        self.print_episode_interval = config["print_episode_interval"]
        self.train_num_episodes_before_next_test = config["train_num_episodes_before_next_test"]
        self.test_num_episodes = config["test_num_episodes"]
        self.episode_reward_avg_solved = config["episode_reward_avg_solved"]

        self.actor = Actor(n_features=3, n_actions=1)
        self.actor_optimizer = optim.Adam(self.actor.parameters(), lr=self.learning_rate)

        self.critic = Critic(n_features=3)
        self.critic_optimizer = optim.Adam(self.critic.parameters(), lr=self.learning_rate)

        self.buffer = Buffer()

        self.time_steps = 0
        self.training_time_steps = 0

    def train_loop(self):
        total_train_start_time = time.time()

        test_episode_reward_avg = -1500
        policy_loss = avg_mu_v = avg_std_v = avg_action = avg_action_prob = 0.0

        is_terminated = False

        for n_episode in range(1, self.max_num_episodes + 1):
            episode_reward = 0

            observation, _ = self.env.reset()

            done = False

            while not done:
                self.time_steps += 1

                action = self.actor.get_action(observation)

                next_observation, reward, terminated, truncated, _ = self.env.step(action * 2)

                episode_reward += reward

                transition = Transition(observation, action, next_observation, reward, terminated)

                self.buffer.append(transition)

                observation = next_observation
                done = terminated or truncated

                if self.time_steps % self.batch_size == 0:
                    policy_loss, avg_mu_v, avg_std_v, avg_action, avg_action_prob = self.train()
                    self.buffer.clear()

            total_training_time = time.time() - total_train_start_time
            total_training_time = time.strftime('%H:%M:%S', time.gmtime(total_training_time))

            if n_episode % self.print_episode_interval == 0:
                print(
                    "[Episode {:3,}, Steps {:6,}]".format(n_episode, self.time_steps),
                    "Episode Reward: {:>9.3f},".format(episode_reward),
                    "Policy Loss: {:>7.3f},".format(policy_loss),
                    "Training Steps: {:5,}, ".format(self.training_time_steps),
                    "Elapsed Time: {}".format(total_training_time)
                )

            if n_episode % self.train_num_episodes_before_next_test == 0:
                test_episode_reward_lst, test_episode_reward_avg = self.test()

                print("[Test Episode Reward: {0}] Average: {1:.3f}".format(
                    test_episode_reward_lst, test_episode_reward_avg
                ))

                if test_episode_reward_avg > self.episode_reward_avg_solved:
                    print("Solved in {0:,} steps ({1:,} training steps)!".format(
                        self.time_steps, self.training_time_steps
                    ))
                    self.model_save(test_episode_reward_avg)
                    is_terminated = True

            if self.use_wandb:
                self.wandb.log({
                    "[TEST] Mean Episode Reward ({0} Episodes)".format(self.test_num_episodes): test_episode_reward_avg,
                    "[TRAIN] Episode Reward": episode_reward,
                    "[TRAIN] Policy Loss": policy_loss,
                    "[TRAIN] avg_mu_v": avg_mu_v,
                    "[TRAIN] avg_std_v": avg_std_v,
                    "[TRAIN] avg_action": avg_action,
                    "[TRAIN] avg_action_prob": avg_action_prob,
                    "Training Episode": n_episode,
                    "Training Steps": self.training_time_steps,
                })

            if is_terminated:
                break

        total_training_time = time.time() - total_train_start_time
        total_training_time = time.strftime('%H:%M:%S', time.gmtime(total_training_time))
        print("Total Training End : {}".format(total_training_time))
        self.wandb.finish()

    def train(self):
        self.training_time_steps += 1

        observations, actions, next_observations, rewards, dones = self.buffer.get()

        ### CRITIC UPDATE
        values = self.critic(observations).squeeze(dim=-1)
        next_values = self.critic(next_observations).squeeze(dim=-1)
        next_values[dones] = 0.0
        q_values = rewards.squeeze(dim=-1) + self.gamma * next_values
        advantages = q_values - values
        critic_loss = F.mse_loss(q_values.detach(), values)
        self.critic_optimizer.zero_grad()
        critic_loss.backward()
        self.critic_optimizer.step()

        ### ACTOR UPDATE
        advantages = (advantages - torch.mean(advantages)) / (torch.std(advantages) + 1e-7)
        mu_v, std_v = self.actor.forward(observations)
        dist = Normal(loc=mu_v, scale=std_v)
        action_log_probs = dist.log_prob(value=actions).squeeze(dim=-1)  # natural log
        entropy = dist.entropy().squeeze(dim=-1)

        log_pi_advantages = action_log_probs * advantages.detach()
        log_pi_advantages_sum = log_pi_advantages.sum()
        entropy_sum = entropy.sum()
        # print(
        #     q_values.shape, values.shape, advantages.shape, values.shape, action_log_probs.shape, log_pi_advantages.shape,
        #     entropy.shape, entropy_sum.shape, log_pi_advantages_sum.shape, "!!!"
        # )

        actor_loss = -1.0 * log_pi_advantages_sum - 1.0 * entropy_sum * self.entropy_beta

        self.actor_optimizer.zero_grad()
        actor_loss.backward()
        self.actor_optimizer.step()

        return (
            actor_loss.item(),
            mu_v.mean().item(),
            std_v.mean().item(),
            actions.type(torch.float32).mean().item(),
            action_log_probs.exp().mean().item()
        )

    def model_save(self, test_episode_reward_avg):
        filename = "a2c_{0}_{1:4.1f}_{2}.pth".format(
            self.env_name, test_episode_reward_avg, self.current_time
        )
        torch.save(self.actor.state_dict(), os.path.join(MODEL_DIR, filename))

        copyfile(
            src=os.path.join(MODEL_DIR, filename),
            dst=os.path.join(MODEL_DIR, "a2c_{0}_latest.pth".format(self.env_name))
        )

    def test(self):
        episode_reward_lst = np.zeros(shape=(self.test_num_episodes,), dtype=float)

        for i in range(self.test_num_episodes):
            episode_reward = 0

            observation, _ = self.test_env.reset()

            done = False

            while not done:
                # action = self.actor.get_action(observation)
                action = self.actor.get_action(observation, exploration=False)

                next_observation, reward, terminated, truncated, _ = self.test_env.step(action * 2)

                episode_reward += reward
                observation = next_observation
                done = terminated or truncated

            episode_reward_lst[i] = episode_reward

        return episode_reward_lst, np.average(episode_reward_lst)


def main():
    ENV_NAME = "Pendulum-v1"

    # env
    env = gym.make(ENV_NAME)
    test_env = gym.make(ENV_NAME)

    config = {
        "env_name": ENV_NAME,                       # ????????? ??????
        "max_num_episodes": 200_000,                # ????????? ?????? ?????? ???????????? ??????
        "batch_size": 32,                           # ????????? ???????????? ????????? ???????????? ?????? ?????? ?????????
        "learning_rate": 0.0003,                    # ?????????
        "gamma": 0.99,                              # ?????????
        "entropy_beta": 0.05,                     # ???????????? ?????????
        "print_episode_interval": 20,               # Episode ?????? ????????? ?????? ???????????? ??????
        "train_num_episodes_before_next_test": 100,                  # ?????? ?????? ?????? ??? ?????? episode ??????
        "test_num_episodes": 3,               # ????????? ???????????? ???????????? ??????
        "episode_reward_avg_solved": -200,          # ?????? ????????? ?????? ????????? ???????????? ???????????? Average
    }

    use_wandb = True
    a2c = A2C(
        env=env, test_env=test_env, config=config, use_wandb=use_wandb
    )
    a2c.train_loop()


if __name__ == '__main__':
    main()
