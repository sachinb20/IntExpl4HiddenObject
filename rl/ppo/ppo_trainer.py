# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.

#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import os
import time
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional
import tqdm
import numpy as np
import torch
import json
from torch.optim.lr_scheduler import LambdaLR
import cv2
import pickle
import random
# [!!] Remove habitat imports
# from habitat import Config, logger
# from habitat.utils.visualizations.utils import observations_to_image
# from habitat_baselines.common.base_trainer import BaseRLTrainer
# from habitat_baselines.common.baseline_registry import baseline_registry
# from habitat_baselines.common.env_utils import construct_envs
# from habitat_baselines.common.environments import get_env_class
# from habitat_baselines.common.rollout_storage import RolloutStorage
# from habitat_baselines.common.tensorboard_utils import TensorboardWriter
# from habitat_baselines.common.utils import (
#     batch_obs,
#     generate_video,
#     linear_decay,
# )
# from habitat_baselines.rl.ppo import PPO, PointNavBaselinePolicy
from .ppo import PPO
from ..common.utils import linear_decay, logger, TensorboardWriter
from ..common.rollout_storage import RolloutStorage
from ..common.env_utils import construct_envs, get_env_class
from ..common.base_trainer import BaseRLTrainer

E2E = os.getenv('E2E')
OBCOV = os.getenv('OBCOV')
HYBRID = os.getenv('HYBRID')

E2E = E2E.lower() == 'true'
OBCOV = OBCOV.lower() == 'true'
HYBRID = HYBRID.lower() == 'true'


# @baseline_registry.register_trainer(name="ppo") # [!!]
class PPOTrainer(BaseRLTrainer):
    r"""Trainer class for PPO algorithm
    Paper: https://arxiv.org/abs/1707.06347.
    """
    # supported_tasks = ["Nav-v0"] # [!!]

    def __init__(self, config=None):
        super().__init__(config)
        self.actor_critic = None
        self.agent = None
        self.envs = None
        if config is not None:
            logger.info(f"config: {config}")

        self._static_encoder = False
        self._encoder = None

    # Create the actor critic model (habitat initializes a PointNavBaselinePolicy)
    def _init_actor_critic_model(self, ppo_cfg):
        raise NotImplementedError

    def _setup_actor_critic_agent(self, ppo_cfg) -> None:
        r"""Sets up actor critic and agent for PPO.

        Args:
            ppo_cfg: config node with relevant params

        Returns:
            None
        """
        logger.add_filehandler(self.config.LOG_FILE)

        # [!!] Requires custom policies for THOR tasks
        # self.actor_critic = PointNavBaselinePolicy(
        #     observation_space=self.envs.observation_spaces[0],
        #     action_space=self.envs.action_spaces[0],
        #     hidden_size=ppo_cfg.hidden_size,
        # )
        self.actor_critic = self._init_actor_critic_model(ppo_cfg)
        self.actor_critic.to(self.device)

        self.agent = PPO(
            actor_critic=self.actor_critic,
            clip_param=ppo_cfg.clip_param,
            ppo_epoch=ppo_cfg.ppo_epoch,
            num_mini_batch=ppo_cfg.num_mini_batch,
            value_loss_coef=ppo_cfg.value_loss_coef,
            entropy_coef=ppo_cfg.entropy_coef,
            lr=ppo_cfg.lr,
            eps=ppo_cfg.eps,
            max_grad_norm=ppo_cfg.max_grad_norm,
            use_normalized_advantage=ppo_cfg.use_normalized_advantage,
        )

    def save_checkpoint(
        self, file_name: str, extra_state: Optional[Dict] = None
    ) -> None:
        r"""Save checkpoint with specified name.

        Args:
            file_name: file name for checkpoint

        Returns:
            None
        """
        checkpoint = {
            "state_dict": self.agent.state_dict(),
            "config": self.config,
        }
        if extra_state is not None:
            checkpoint["extra_state"] = extra_state

        torch.save(
            checkpoint, os.path.join(self.config.CHECKPOINT_FOLDER, file_name)
        )

    def load_checkpoint(self, checkpoint_path: str, *args, **kwargs) -> Dict:
        r"""Load checkpoint of specified path as a dict.

        Args:
            checkpoint_path: path of target checkpoint
            *args: additional positional args
            **kwargs: additional keyword args

        Returns:
            dict containing checkpoint info
        """
        return torch.load(checkpoint_path, *args, **kwargs)

    METRICS_BLACKLIST = {"top_down_map", "collisions.is_collision"}

    @classmethod
    def _extract_scalars_from_info(
        cls, info: Dict[str, Any]
    ) -> Dict[str, float]:
        result = {}
        for k, v in info.items():
            if k in cls.METRICS_BLACKLIST:
                continue

            if isinstance(v, dict):
                result.update(
                    {
                        k + "." + subk: subv
                        for subk, subv in cls._extract_scalars_from_info(
                            v
                        ).items()
                        if (k + "." + subk) not in cls.METRICS_BLACKLIST
                    }
                )
            # Things that are scalar-like will have an np.size of 1.
            # Strings also have an np.size of 1, so explicitly ban those
            elif np.size(v) == 1 and not isinstance(v, str):
                result[k] = float(v)

        return result

    @classmethod
    def _extract_scalars_from_infos(
        cls, infos: List[Dict[str, Any]]
    ) -> Dict[str, List[float]]:

        results = defaultdict(list)
        for i in range(len(infos)):
            for k, v in cls._extract_scalars_from_info(infos[i]).items():
                results[k].append(v)

        return results

    # [++] process observations before batching them
    def batch_obs(self, observations, device=None):
        raise NotImplementedError


    def _collect_rollout_step(
        self, rollouts, current_episode_reward, running_episode_stats
    ):
        pth_time = 0.0
        env_time = 0.0

        t_sample_action = time.time()
        # sample actions
        with torch.no_grad():
            step_observation = {
                k: v[rollouts.step] for k, v in rollouts.observations.items()
            }

            (
                values,
                actions,
                actions_log_probs,
                recurrent_hidden_states,
            ) = self.actor_critic.act(
                step_observation,
                rollouts.recurrent_hidden_states[rollouts.step],
                rollouts.prev_actions[rollouts.step],
                rollouts.masks[rollouts.step],
            )

        pth_time += time.time() - t_sample_action

        t_step_env = time.time()

        outputs = self.envs.step([a[0].item() for a in actions])
        observations, rewards, dones, infos = [list(x) for x in zip(*outputs)]
        # print(actions)
        # if not dones[0]: 
        #     
        #     if rewards[0]== 1 and (actions==5).any().item() == 5:
        #         self.envs.act("close")
        #         print("ttttttttgthyhy")


        env_time += time.time() - t_step_env

        t_update_stats = time.time()
        batch = self.batch_obs(observations, device=self.device)
        rewards = torch.tensor(
            rewards, dtype=torch.float, device=current_episode_reward.device
        )
        rewards = rewards.unsqueeze(1)

        masks = torch.tensor(
            [[0.0] if done else [1.0] for done in dones],
            dtype=torch.float,
            device=current_episode_reward.device,
        )

        current_episode_reward += rewards
        running_episode_stats["reward"] += (1 - masks) * current_episode_reward
        running_episode_stats["count"] += 1 - masks
        for k, v in self._extract_scalars_from_infos(infos).items():
            v = torch.tensor(
                v, dtype=torch.float, device=current_episode_reward.device
            ).unsqueeze(1)
            if k not in running_episode_stats:
                running_episode_stats[k] = torch.zeros_like(
                    running_episode_stats["count"]
                )

            running_episode_stats[k] += (1 - masks) * v

        current_episode_reward *= masks

        if self._static_encoder:
            with torch.no_grad():
                batch["visual_features"] = self._encoder(batch)

        rollouts.insert(
            batch,
            recurrent_hidden_states,
            actions,
            actions_log_probs,
            values,
            rewards,
            masks,
        )

        pth_time += time.time() - t_update_stats

        return pth_time, env_time, self.envs.num_envs

    def _update_agent(self, ppo_cfg, rollouts):
        t_update_model = time.time()
        with torch.no_grad():
            last_observation = {
                k: v[rollouts.step] for k, v in rollouts.observations.items()
            }
            next_value = self.actor_critic.get_value(
                last_observation,
                rollouts.recurrent_hidden_states[rollouts.step],
                rollouts.prev_actions[rollouts.step],
                rollouts.masks[rollouts.step],
            ).detach()

        rollouts.compute_returns(
            next_value, ppo_cfg.use_gae, ppo_cfg.gamma, ppo_cfg.tau
        )

        value_loss, action_loss, dist_entropy = self.agent.update(rollouts)

        rollouts.after_update()

        return (
            time.time() - t_update_model,
            value_loss,
            action_loss,
            dist_entropy,
        )

    # [!!] Allow subclasses to create modified rollout storages
    def create_rollout_storage(self, ppo_cfg):
        rollouts = RolloutStorage(
            ppo_cfg.num_steps,
            self.envs.num_envs,
            self.envs.observation_spaces[0],
            self.envs.action_spaces[0],
            ppo_cfg.hidden_size,
        )
        return rollouts

    def train(self) -> None:
        r"""Main method for training PPO.

        Returns:
            None
        """
        print("prelauda")
        self.envs = construct_envs(
            self.config, get_env_class(self.config.ENV.ENV_NAME)
        )
        print("lauda")
        ppo_cfg = self.config.RL.PPO
        self.device = (
            torch.device("cuda", self.config.TORCH_GPU_ID)
            if torch.cuda.is_available()
            else torch.device("cpu")
        )
        if not os.path.isdir(self.config.CHECKPOINT_FOLDER):
            os.makedirs(self.config.CHECKPOINT_FOLDER)
        self._setup_actor_critic_agent(ppo_cfg)
        logger.info(
            "agent number of parameters: {}".format(
                sum(param.numel() for param in self.agent.parameters())
            )
        )

        if self.config.LOAD is not None:
            ckpt_dict = self.load_checkpoint(self.config.LOAD, map_location="cpu")
            self.agent.load_state_dict(ckpt_dict["state_dict"])
            self.actor_critic = self.agent.actor_critic

        # [!!] Allow subclasses to create modified rollout storages
        rollouts = self.create_rollout_storage(ppo_cfg)
        rollouts.to(self.device)

        observations = self.envs.reset()
        # print("@@@@@@@@@@@@@@@@@@")
        # print(observations[0]["rgb"].shape)
        batch = self.batch_obs(observations, device=self.device)
        # print("$$$$$$$$$$$$$$$$$$$")
        # print(batch['rgb'].shape)
        for sensor in rollouts.observations:
            rollouts.observations[sensor][0].copy_(batch[sensor])

        # batch and observations may contain shared PyTorch CUDA
        # tensors.  We must explicitly clear them here otherwise
        # they will be kept in memory for the entire duration of training!
        batch = None
        observations = None

        current_episode_reward = torch.zeros(self.envs.num_envs, 1)
        running_episode_stats = dict(
            count=torch.zeros(self.envs.num_envs, 1),
            reward=torch.zeros(self.envs.num_envs, 1),
        )
        window_episode_stats = defaultdict(
            lambda: deque(maxlen=ppo_cfg.reward_window_size)
        )

        t_start = time.time()
        env_time = 0
        pth_time = 0
        count_steps = 0
        count_checkpoints = 0

        lr_scheduler = LambdaLR(
            optimizer=self.agent.optimizer,
            lr_lambda=lambda x: linear_decay(x, self.config.NUM_UPDATES),
        )

        with TensorboardWriter(
            self.config.TENSORBOARD_DIR, flush_secs=self.flush_secs
        ) as writer:
            for update in range(self.config.NUM_UPDATES):
                if ppo_cfg.use_linear_lr_decay:
                    lr_scheduler.step()

                if ppo_cfg.use_linear_clip_decay:
                    self.agent.clip_param = ppo_cfg.clip_param * linear_decay(
                        update, self.config.NUM_UPDATES
                    )

                for step in tqdm.tqdm(range(ppo_cfg.num_steps)): # [!!] Add tqdm
                    (
                        delta_pth_time,
                        delta_env_time,
                        delta_steps,
                    ) = self._collect_rollout_step(
                        rollouts, current_episode_reward, running_episode_stats
                    )
                    pth_time += delta_pth_time
                    env_time += delta_env_time
                    count_steps += delta_steps

                (
                    delta_pth_time,
                    value_loss,
                    action_loss,
                    dist_entropy,
                ) = self._update_agent(ppo_cfg, rollouts)
                pth_time += delta_pth_time

                for k, v in running_episode_stats.items():
                    window_episode_stats[k].append(v.clone())

                deltas = {
                    k: (
                        (v[-1] - v[0]).sum().item()
                        if len(v) > 1
                        else v[0].sum().item()
                    )
                    for k, v in window_episode_stats.items()
                }
                deltas["count"] = max(deltas["count"], 1.0)

                writer.add_scalar(
                    "reward", deltas["reward"] / deltas["count"], count_steps
                )

                # [!!] Write policy/value/dist_entropy losses directly
                writer.add_scalar('policy_loss', action_loss, count_steps)
                writer.add_scalar('value_loss', value_loss, count_steps)
                writer.add_scalar('dist_entropy', dist_entropy, count_steps)


                # # Check to see if there are any metrics
                # # that haven't been logged yet
                # metrics = {
                #     k: v / deltas["count"]
                #     for k, v in deltas.items()
                #     if k not in {"reward", "count"}
                # }
                # if len(metrics) > 0:
                #     writer.add_scalars("metrics", metrics, count_steps)

                # losses = [value_loss, action_loss]
                # writer.add_scalars(
                #     "losses",
                #     {k: l for l, k in zip(losses, ["value", "policy"])},
                #     count_steps,
                # )

                # log stats
                if update > 0 and update % self.config.LOG_INTERVAL == 0:
                    logger.info(
                        "update: {}\tfps: {:.3f}\t".format(
                            update, count_steps / (time.time() - t_start)
                        )
                    )

                    logger.info(
                        "update: {}\tenv-time: {:.3f}s\tpth-time: {:.3f}s\t"
                        "frames: {}".format(
                            update, env_time, pth_time, count_steps
                        )
                    )

                    logger.info(
                        "Average window size: {}  {}".format(
                            len(window_episode_stats["count"]),
                            "  ".join(
                                "{}: {:.3f}".format(k, v / deltas["count"])
                                for k, v in deltas.items()
                                if k != "count"
                            ),
                        )
                    )

                # checkpoint model
                if update % self.config.CHECKPOINT_INTERVAL == 0:
                    self.save_checkpoint(
                        f"ckpt.{count_checkpoints}.pth", dict(step=count_steps)
                    )
                    count_checkpoints += 1

            self.envs.close()

    def get_action(self,index, act_to_idx):
        for action, idx in act_to_idx.items():
            if idx == index:
                return action
        return None

    def eval(self) -> None:

        if E2E:
            os.makedirs(os.path.join(self.config.CHECKPOINT_FOLDER, 'eval/'), exist_ok=True)

            # add test episode information to config
            test_episodes = json.load(open(self.config.EVAL.DATASET))
            self.config.defrost()
            self.config.ENV.TEST_EPISODES = test_episodes
            self.config.ENV.TEST_EPISODE_COUNT = len(test_episodes)
            self.config.freeze()

            # Map location CPU is almost always better than mapping to a CUDA device.
            checkpoint_path = self.config.LOAD
            ckpt_dict = self.load_checkpoint(checkpoint_path, map_location="cpu")
            ppo_cfg = self.config.RL.PPO

            logger.info(f"env config: {self.config}")
            self.envs = construct_envs(self.config, get_env_class(self.config.ENV.ENV_NAME))
            self._setup_actor_critic_agent(ppo_cfg)

            # [!!] Log extra stuff
            logger.info(checkpoint_path)
            logger.info(f"num_steps: {self.config.ENV.NUM_STEPS}")

            # [!!] Only load if present
            if ckpt_dict is not None:
                self.agent.load_state_dict(ckpt_dict["state_dict"])
            else:
                logger.info('NO CHECKPOINT LOADED!')
            self.actor_critic = self.agent.actor_critic

            observations = self.envs.reset()
            batch = self.batch_obs(observations, self.device)

            current_episode_reward = torch.zeros(
                self.envs.num_envs, 1, device=self.device
            )

            test_recurrent_hidden_states = torch.zeros(
                self.actor_critic.net.num_recurrent_layers,
                self.config.NUM_PROCESSES,
                ppo_cfg.hidden_size,
                device=self.device,
            )
            prev_actions = torch.zeros(
                self.config.NUM_PROCESSES, 1, device=self.device, dtype=torch.long
            )
            not_done_masks = torch.zeros(
                self.config.NUM_PROCESSES, 1, device=self.device
            )
            stats_episodes = dict()  # dict of dicts that stores stats per episode

            rgb_frames = [
                [] for _ in range(self.config.NUM_PROCESSES)
            ]  # type: List[List[np.ndarray]]

            # [!!] Store extra information about the trajectory that the env does not return
            episode_infos = [[] for _ in range(self.config.NUM_PROCESSES)]

            pbar = tqdm.tqdm()
            self.actor_critic.eval()
            print("@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@")
            print(self.config.ENV.TEST_EPISODE_COUNT)
            print("@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@")

            action_list = []
            observation_list = []
            prev_obs = [None]
            metadata_list = []
            obj_cov_step=[]
            obj_pick_step=[]
            step_count = 0
            prev_obs = [None]
            last_five_actions = deque(maxlen=5)
            while (
                len(stats_episodes) < self.config.ENV.TEST_EPISODE_COUNT
                and self.envs.num_envs > 0
            ):

                # [!!] Show more fine-grained progress. THOR is slow!
                pbar.update()
                
                current_episodes = self.envs.current_episodes()

                with torch.no_grad():
                    (
                        _,
                        actions,
                        _,
                        test_recurrent_hidden_states,
                    ) = self.actor_critic.act(
                        batch,
                        test_recurrent_hidden_states,
                        prev_actions,
                        not_done_masks,
                        deterministic=False,
                    )

                    prev_actions.copy_(actions)
                
                outputs = self.envs.step([a[0].item() for a in actions])
                observations, rewards, dones, infos = [
                    list(x) for x in zip(*outputs)
                ]
                step_count +=1
                batch = self.batch_obs(observations, self.device)

                not_done_masks = torch.tensor(
                    [[0.0] if done else [1.0] for done in dones],
                    dtype=torch.float,
                    device=self.device,
                )
                act_to_idx = {'forward': 0, 'up': 1, 'down': 2, 'tright': 3, 'tleft': 4,'take':5, 'put':6, 'open': 7, 'close': 8}


                # last_five_actions.append([infos[0]["action"],infos[0]["success"]])
                # if len(last_five_actions) == 5 and all(x == last_five_actions[0] for x in last_five_actions):
                #     print("horiya")
                #     times = random.randint(1, 3)  # Randomly choose to call 1, 2, or 3 times
                #     for _ in range(times):
                #         result = self.envs.act("tright")
                #     self.envs.act("forward")
                #     self.envs.act("forward")
                #     self.envs.act("forward")
                #     self.envs.act("forward")                               
                    

                if not dones[0]:

                    if infos[0]["success"]:#more exploration observed
                    # if :
                        # if self.get_action(actions.item(),act_to_idx) == "open" or self.get_action(actions.item(),act_to_idx) == "close":
                        # cv2.imwrite('{}_{}.png'.format(self.get_action(actions.item(),act_to_idx), '2'), observations[0]["rgb"])
                        # cv2.imwrite('{}_{}.png'.format(self.get_action(actions.item(),act_to_idx), '1'), prev_obs[0]["rgb"])
                        if rewards[0]== 1 and self.get_action(actions.item(),act_to_idx)=="open":
                            print("badiya")
                            obj_cov_step.append(step_count)
                                           
                            action_list.append("open")
                            observation_list.append([prev_obs[0],observations[0]])
                            metadata_list.append([infos[0]["prev_metadata"],infos[0]["next_metadata"]]) 


                        if (rewards[0]== 2 or rewards[0]== 5) and self.get_action(actions.item(),act_to_idx)=="take":#more exploration observed
                        # if infos[0]["success"]:
                            # if self.get_action(actions.item(),act_to_idx) == "open" or self.get_action(actions.item(),act_to_idx) == "close":
                            # cv2.imwrite('{}_{}.png'.format(self.get_action(actions.item(),act_to_idx), '2'), observations[0]["rgb"])
                            # cv2.imwrite('{}_{}.png'.format(self.get_action(actions.item(),act_to_idx), '1'), prev_obs[0]["rgb"])
                            if self.get_action(actions.item(),act_to_idx)=="take":
                                print("bbhot badiya")
                                obj_pick_step.append(step_count)

                            action_list.append("take")
                            observation_list.append([prev_obs[0],observations[0]])
                            metadata_list.append([infos[0]["prev_metadata"],infos[0]["next_metadata"]]) 
                                                            


                if dones[0]:

                    scene = current_episodes[0]['scene_id']
                    episode = current_episodes[0]['episode_id']

                    # Create filename
                    filename = f"E2E_rollouts/{scene}_{episode}.pkl"

                    # Data to save
                    data_to_save = {'action_list': action_list, 'observation_list': observation_list,"obj_cov_step":obj_cov_step,"obj_pick_step":obj_pick_step,"metadata_list":metadata_list}  # Replace with your actual data

                    # Save data to pickle file
                    with open(filename, 'wb') as f:
                        pickle.dump(data_to_save, f)

                    action_list = []
                    observation_list = [] 
                    metadata_list = []
                    obj_cov_step = []
                    obj_pick_step = []


                rewards = torch.tensor(
                    rewards, dtype=torch.float, device=self.device
                ).unsqueeze(1)

                current_episode_reward += rewards

                # # [!!] store epiode history
                # for i in range(self.envs.num_envs):
                #     episode_infos[i].append(infos[i])


                next_episodes = self.envs.current_episodes()
                envs_to_pause = []
                n_envs = self.envs.num_envs
                prev_obs = observations
                # for i in range(n_envs):

                #     if (
                #         next_episodes[i]['scene_id'],
                #         next_episodes[i]['episode_id'],
                #     ) in stats_episodes:
                #         envs_to_pause.append(i)

                #     # episode ended
                #     if not_done_masks[i].item() == 0:
                #         # pbar.update()
                #         episode_stats = dict()
                #         episode_stats["reward"] = current_episode_reward[i].item()
                #         episode_stats.update(
                #             self._extract_scalars_from_info(infos[i])
                #         )
                #         current_episode_reward[i] = 0


                #         # [!!] Add per-step episode information
                #         episode_info = []
                #         for info in episode_infos[i]:
                #             act_data = {'reward': info['reward'], 'action': info['action'], 'target': None, 'success': info['success']} 
                #             if 'target' in info:
                #                 act_data['target'] = info['target']['objectId']
                #             episode_info.append(act_data)
                #         episode_stats['step_info'] = episode_info
                #         episode_infos[i] = []

                #         # use scene_id + episode_id as unique id for storing stats
                #         stats_episodes[
                #             (
                #                 current_episodes[i]['scene_id'],
                #                 current_episodes[i]['episode_id'],
                #             )
                #         ] = episode_stats

                #         # [!!] Save episode data in the eval folder for processing
                #         scene, episode = current_episodes[i]['scene_id'], current_episodes[i]['episode_id']
                #         torch.save({'scene_id':scene,
                #                     'episode_id':episode,
                #                     'stats':episode_stats},
                #                     f'{self.config.CHECKPOINT_FOLDER}/eval/{scene}_{episode}.pth')
                        
                


                (
                    self.envs,
                    test_recurrent_hidden_states,
                    not_done_masks,
                    current_episode_reward,
                    prev_actions,
                    batch,
                    rgb_frames,
                ) = self._pause_envs(
                    envs_to_pause,
                    self.envs,
                    test_recurrent_hidden_states,
                    not_done_masks,
                    current_episode_reward,
                    prev_actions,
                    batch,
                    rgb_frames,
                )

            num_episodes = len(stats_episodes)
            aggregated_stats = dict()
            # for stat_key in next(iter(stats_episodes.values())).keys(): # [!!] Only output reward
            for stat_key in ['reward']:
                aggregated_stats[stat_key] = (
                    sum([v[stat_key] for v in stats_episodes.values()])
                    / num_episodes
                )

            for k, v in aggregated_stats.items():
                logger.info(f"Average episode {k}: {v:.4f}")

            self.envs.close()
        if HYBRID:
            os.makedirs(os.path.join(self.config.CHECKPOINT_FOLDER, 'eval/'), exist_ok=True)

            # add test episode information to config
            test_episodes = json.load(open(self.config.EVAL.DATASET))
            self.config.defrost()
            self.config.ENV.TEST_EPISODES = test_episodes
            self.config.ENV.TEST_EPISODE_COUNT = len(test_episodes)
            self.config.freeze()

            # Map location CPU is almost always better than mapping to a CUDA device.
            checkpoint_path = self.config.LOAD
            ckpt_dict = self.load_checkpoint(checkpoint_path, map_location="cpu")
            ppo_cfg = self.config.RL.PPO

            logger.info(f"env config: {self.config}")
            self.envs = construct_envs(self.config, get_env_class(self.config.ENV.ENV_NAME))
            self._setup_actor_critic_agent(ppo_cfg)

            # [!!] Log extra stuff
            logger.info(checkpoint_path)
            logger.info(f"num_steps: {self.config.ENV.NUM_STEPS}")

            # [!!] Only load if present
            if ckpt_dict is not None:
                self.agent.load_state_dict(ckpt_dict["state_dict"])
            else:
                logger.info('NO CHECKPOINT LOADED!')
            self.actor_critic = self.agent.actor_critic

            observations = self.envs.reset()
            batch = self.batch_obs(observations, self.device)

            current_episode_reward = torch.zeros(
                self.envs.num_envs, 1, device=self.device
            )

            test_recurrent_hidden_states = torch.zeros(
                self.actor_critic.net.num_recurrent_layers,
                self.config.NUM_PROCESSES,
                ppo_cfg.hidden_size,
                device=self.device,
            )
            prev_actions = torch.zeros(
                self.config.NUM_PROCESSES, 1, device=self.device, dtype=torch.long
            )
            not_done_masks = torch.zeros(
                self.config.NUM_PROCESSES, 1, device=self.device
            )
            stats_episodes = dict()  # dict of dicts that stores stats per episode

            rgb_frames = [
                [] for _ in range(self.config.NUM_PROCESSES)
            ]  # type: List[List[np.ndarray]]

            # [!!] Store extra information about the trajectory that the env does not return
            episode_infos = [[] for _ in range(self.config.NUM_PROCESSES)]

            pbar = tqdm.tqdm()
            self.actor_critic.eval()
            print("@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@")
            print(self.config.ENV.TEST_EPISODE_COUNT)
            print("@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@")
            action_list = []
            observation_list = []
            prev_obs = [None]
            last_five_actions = deque(maxlen=5)
            metadata_list = []
            obj_cov_step=[]
            obj_pick_step=[]
            step_count = 0
            while (
                len(stats_episodes) < self.config.ENV.TEST_EPISODE_COUNT
                and self.envs.num_envs > 0
            ):

                # [!!] Show more fine-grained progress. THOR is slow!
                pbar.update()
                
                current_episodes = self.envs.current_episodes()

                with torch.no_grad():
                    (
                        _,
                        actions,
                        _,
                        test_recurrent_hidden_states,
                    ) = self.actor_critic.act(
                        batch,
                        test_recurrent_hidden_states,
                        prev_actions,
                        not_done_masks,
                        deterministic=False,
                    )

                    prev_actions.copy_(actions)
                
                outputs = self.envs.step([a[0].item() for a in actions])
                observations, rewards, dones, infos = [
                    list(x) for x in zip(*outputs)
                ]
                step_count+=1
                batch = self.batch_obs(observations, self.device)

                not_done_masks = torch.tensor(
                    [[0.0] if done else [1.0] for done in dones],
                    dtype=torch.float,
                    device=self.device,
                )
                act_to_idx = {'forward': 0, 'up': 1, 'down': 2, 'tright': 3, 'tleft': 4, 'open': 5, 'close': 6}
                # print(infos[0]["action"],infos[0]["success"])

                last_five_actions.append([infos[0]["action"],infos[0]["success"]])
                if len(last_five_actions) == 5 and all(x == last_five_actions[0] for x in last_five_actions):
                    print("horiya")
                    times = random.randint(1, 3)  # Randomly choose to call 1, 2, or 3 times
                    for _ in range(times):
                        result = self.envs.act("tright")
                    self.envs.act("forward")
                    self.envs.act("forward")
                    self.envs.act("forward")
                    self.envs.act("forward")                               
                    

                if not dones[0]:

                    # if rewards[0]== 1:#more exploration observed
                    if infos[0]["success"]:
                        # if self.get_action(actions.item(),act_to_idx) == "open" or self.get_action(actions.item(),act_to_idx) == "close":
                        #     # cv2.imwrite('{}_{}.png'.format(self.get_action(actions.item(),act_to_idx), '2'), observations[0]["rgb"])
                        #     # cv2.imwrite('{}_{}.png'.format(self.get_action(actions.item(),act_to_idx), '1'), prev_obs[0]["rgb"])
                        #     action_list.append(self.get_action(actions.item(),act_to_idx))
                        #     observation_list.append([prev_obs[0],observations[0]])
                        #     metadata_list.append([info["prev_metadata"],info["next_metadata"]]) 

                        if self.get_action(actions.item(),act_to_idx) == "open":
                            if rewards[0]== 1:
                                print("badiya")
                                obj_cov_step.append(step_count)
                                action_list.append("open")
                                observation_list.append([prev_obs[0],observations[0]])
                                metadata_list.append([info["prev_metadata"],info["next_metadata"]]) 

                        info = self.envs.act("take")
                        if info["success"]:
                            print("bhot badiya")
                            obj_pick_step.append(step_count)
                            action_list.append("take")
                            observation_list.append([info["prev_obs"]["rgb"],info["next_obs"]["rgb"]])
                            metadata_list.append([info["prev_metadata"],info["next_metadata"]]) 


                        info = self.envs.act("put")

                                


                        self.envs.act("close")
                            
                                                        


                if dones[0]:

                    scene = current_episodes[0]['scene_id']
                    episode = current_episodes[0]['episode_id']

                    # Create filename
                    filename = f"Hybrid_rollouts/{scene}_{episode}.pkl"

                    # Data to save
                    data_to_save = {'action_list': action_list, 'observation_list': observation_list,"obj_cov_step":obj_cov_step,"obj_pick_step":obj_pick_step,"metadata_list":metadata_list}  # Replace with your actual data

                    # Save data to pickle file
                    with open(filename, 'wb') as f:
                        pickle.dump(data_to_save, f)

                    action_list = []
                    observation_list = [] 
                    metadata_list = []
                    obj_cov_step = []
                    obj_pick_step = []


                rewards = torch.tensor(
                    rewards, dtype=torch.float, device=self.device
                ).unsqueeze(1)

                current_episode_reward += rewards

                # # [!!] store epiode history
                # for i in range(self.envs.num_envs):
                #     episode_infos[i].append(infos[i])


                next_episodes = self.envs.current_episodes()
                envs_to_pause = []
                n_envs = self.envs.num_envs
                prev_obs = observations
                # for i in range(n_envs):

                #     if (
                #         next_episodes[i]['scene_id'],
                #         next_episodes[i]['episode_id'],
                #     ) in stats_episodes:
                #         envs_to_pause.append(i)

                #     # episode ended
                #     if not_done_masks[i].item() == 0:
                #         # pbar.update()
                #         episode_stats = dict()
                #         episode_stats["reward"] = current_episode_reward[i].item()
                #         episode_stats.update(
                #             self._extract_scalars_from_info(infos[i])
                #         )
                #         current_episode_reward[i] = 0


                #         # [!!] Add per-step episode information
                #         episode_info = []
                #         for info in episode_infos[i]:
                #             act_data = {'reward': info['reward'], 'action': info['action'], 'target': None, 'success': info['success']} 
                #             if 'target' in info:
                #                 act_data['target'] = info['target']['objectId']
                #             episode_info.append(act_data)
                #         episode_stats['step_info'] = episode_info
                #         episode_infos[i] = []

                #         # use scene_id + episode_id as unique id for storing stats
                #         stats_episodes[
                #             (
                #                 current_episodes[i]['scene_id'],
                #                 current_episodes[i]['episode_id'],
                #             )
                #         ] = episode_stats

                #         # [!!] Save episode data in the eval folder for processing
                #         scene, episode = current_episodes[i]['scene_id'], current_episodes[i]['episode_id']
                #         torch.save({'scene_id':scene,
                #                     'episode_id':episode,
                #                     'stats':episode_stats},
                #                     f'{self.config.CHECKPOINT_FOLDER}/eval/{scene}_{episode}.pth')
                        
                


                (
                    self.envs,
                    test_recurrent_hidden_states,
                    not_done_masks,
                    current_episode_reward,
                    prev_actions,
                    batch,
                    rgb_frames,
                ) = self._pause_envs(
                    envs_to_pause,
                    self.envs,
                    test_recurrent_hidden_states,
                    not_done_masks,
                    current_episode_reward,
                    prev_actions,
                    batch,
                    rgb_frames,
                )

            num_episodes = len(stats_episodes)
            aggregated_stats = dict()
            # for stat_key in next(iter(stats_episodes.values())).keys(): # [!!] Only output reward
            for stat_key in ['reward']:
                aggregated_stats[stat_key] = (
                    sum([v[stat_key] for v in stats_episodes.values()])
                    / num_episodes
                )

            for k, v in aggregated_stats.items():
                logger.info(f"Average episode {k}: {v:.4f}")

            self.envs.close()

        if OBCOV:
            os.makedirs(os.path.join(self.config.CHECKPOINT_FOLDER, 'eval/'), exist_ok=True)

            # add test episode information to config
            test_episodes = json.load(open(self.config.EVAL.DATASET))
            self.config.defrost()
            self.config.ENV.TEST_EPISODES = test_episodes
            self.config.ENV.TEST_EPISODE_COUNT = len(test_episodes)
            self.config.freeze()

            # Map location CPU is almost always better than mapping to a CUDA device.
            checkpoint_path = self.config.LOAD
            ckpt_dict = self.load_checkpoint(checkpoint_path, map_location="cpu")
            ppo_cfg = self.config.RL.PPO

            logger.info(f"env config: {self.config}")
            self.envs = construct_envs(self.config, get_env_class(self.config.ENV.ENV_NAME))
            self._setup_actor_critic_agent(ppo_cfg)

            # [!!] Log extra stuff
            logger.info(checkpoint_path)
            logger.info(f"num_steps: {self.config.ENV.NUM_STEPS}")

            # [!!] Only load if present
            if ckpt_dict is not None:
                self.agent.load_state_dict(ckpt_dict["state_dict"])
            else:
                logger.info('NO CHECKPOINT LOADED!')
            self.actor_critic = self.agent.actor_critic

            observations = self.envs.reset()
            batch = self.batch_obs(observations, self.device)

            current_episode_reward = torch.zeros(
                self.envs.num_envs, 1, device=self.device
            )

            test_recurrent_hidden_states = torch.zeros(
                self.actor_critic.net.num_recurrent_layers,
                self.config.NUM_PROCESSES,
                ppo_cfg.hidden_size,
                device=self.device,
            )
            prev_actions = torch.zeros(
                self.config.NUM_PROCESSES, 1, device=self.device, dtype=torch.long
            )
            not_done_masks = torch.zeros(
                self.config.NUM_PROCESSES, 1, device=self.device
            )
            stats_episodes = dict()  # dict of dicts that stores stats per episode

            rgb_frames = [
                [] for _ in range(self.config.NUM_PROCESSES)
            ]  # type: List[List[np.ndarray]]

            # [!!] Store extra information about the trajectory that the env does not return
            episode_infos = [[] for _ in range(self.config.NUM_PROCESSES)]

            pbar = tqdm.tqdm()
            self.actor_critic.eval()
            print("@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@")
            print(self.config.ENV.TEST_EPISODE_COUNT)
            print("@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@")
            action_list = []
            observation_list = []
            metadata_list = []
            obj_cov_step=[]
            obj_pick_step=[]
            last_five_actions = deque(maxlen=5)
            last_five_success = deque(maxlen=5)
            step_count = 0
            while (
                len(stats_episodes) < self.config.ENV.TEST_EPISODE_COUNT
                and self.envs.num_envs > 0
            ):

                # [!!] Show more fine-grained progress. THOR is slow!
                pbar.update()
                
                current_episodes = self.envs.current_episodes()

                with torch.no_grad():
                    (
                        _,
                        actions,
                        _,
                        test_recurrent_hidden_states,
                    ) = self.actor_critic.act(
                        batch,
                        test_recurrent_hidden_states,
                        prev_actions,
                        not_done_masks,
                        deterministic=False,
                    )

                    prev_actions.copy_(actions)
                
                outputs = self.envs.step([a[0].item() for a in actions])
                observations, rewards, dones, infos = [
                    list(x) for x in zip(*outputs)
                ]
                
                batch = self.batch_obs(observations, self.device)

                not_done_masks = torch.tensor(
                    [[0.0] if done else [1.0] for done in dones],
                    dtype=torch.float,
                    device=self.device,
                )
                step_count+=1
                # print(infos[0]["action"],infos[0]["success"])
                last_five_actions.append([infos[0]["action"],infos[0]["success"]])
                last_five_success.append(infos[0]["success"])
                # print(last_five_success)
                # if len(last_five_actions) == 5 and (all(x == last_five_actions[0] for x in last_five_actions) or all(x == last_five_success[0] for x in last_five_success)):
                #     print("horiya")
                #     self.envs.act("close")
                #     self.envs.act("put") 
                #     self.envs.act("up") 
                #     self.envs.act("close")
                #     self.envs.act("put") 
                #     self.envs.act("down") 
                #     self.envs.act("down") 
                #     self.envs.act("close") 
                #     self.envs.act("put")

                #     times = random.randint(1, 3)  # Randomly choose to call 1, 2, or 3 times
                #     for _ in range(times):
                #         result = self.envs.act("tright")
                #     self.envs.act("forward")
                #     self.envs.act("forward")
                #     self.envs.act("forward")
                #     self.envs.act("forward")  
                #     # self.envs.act("close") 
                                                

                # print(infos[0]['traj_masks'])
                # act_to_idx = {'forward': 0, 'up': 1, 'down': 2, 'tright': 3, 'tleft': 4, 'take': 5, 'put': 6, 'open': 7, 'close': 8, 'toggle-on': 9, 'toggle-off': 10, 'slice': 11}
                act_to_idx = {'forward': 0, 'up': 1, 'down': 2, 'tright': 3, 'tleft': 4, 'open': 5, 'close': 6}
                # print(type(observations[0]["rgb"]))
                # print(actions)
                # print(rewards)
                # print(infos[0])
                # print(dones)
                if not dones[0]:
                    # print(actions.item())
                    if rewards[0]== 1:
                        print("hmm")
                        print(step_count)
                        obj_cov_step.append(step_count)
                        info = self.envs.act("open")
                        if info["success"]:
                            action_list.append("open")
                            observation_list.append([info["prev_obs"]["rgb"],info["next_obs"]["rgb"]])
                            metadata_list.append([info["prev_metadata"],info["next_metadata"]])

                        info = self.envs.act("take")

                        if info["success"]:
                            print("wall done")
                            obj_pick_step.append(step_count)
                            action_list.append("take")
                            observation_list.append([info["prev_obs"]["rgb"],info["next_obs"]["rgb"]])
                            metadata_list.append([info["prev_metadata"],info["next_metadata"]])  

                        self.envs.act("put")
                        self.envs.act("close")
                    #     # if self.get_action(actions.item(),act_to_idx) == "take" or self.get_action(actions.item(),act_to_idx) == "put" or self.get_action(actions.item(),act_to_idx) == "open" or self.get_action(actions.item(),act_to_idx) == "close":
                    #     if self.get_action(actions.item(),act_to_idx) == "open" or self.get_action(actions.item(),act_to_idx) == "close":
                    #     # cv2.imwrite('{}_{}.png'.format(self.get_action(actions.item(),act_to_idx), '2'), observations[0]["rgb"])
                    #         # cv2.imwrite('{}_{}.png'.format(self.get_action(actions.item(),act_to_idx), '1'), prev_obs[0]["rgb"])



                        outputs = self.envs.step([a[0].item() for a in actions])
                        observations, rewards, dones, infos = [
                            list(x) for x in zip(*outputs)
                        ]
                        
                        batch = self.batch_obs(observations, self.device)

                        not_done_masks = torch.tensor(
                            [[0.0] if done else [1.0] for done in dones],
                            dtype=torch.float,
                            device=self.device,
                        )

                        # action_list.append(self.get_action(actions.item(),act_to_idx))
                        # observation_list.append([prev_obs[0],observations[0]])





                if dones[0]:

                    scene = current_episodes[0]['scene_id']
                    episode = current_episodes[0]['episode_id']

                    # Create filename
                    filename = f"{scene}_{episode}.pkl"

                    # Data to save
                    data_to_save = {'action_list': action_list, 'observation_list': observation_list,"obj_cov_step":obj_cov_step,"obj_pick_step":obj_pick_step,"metadata_list":metadata_list}  # Replace with your actual data

                    # Save data to pickle file
                    with open(filename, 'wb') as f:
                        pickle.dump(data_to_save, f)

                    action_list = []
                    observation_list = [] 
                    metadata_list = []
                    obj_cov_step = []
                    obj_pick_step = []



                rewards = torch.tensor(
                    rewards, dtype=torch.float, device=self.device
                ).unsqueeze(1)

                current_episode_reward += rewards

                # # [!!] store epiode history
                # for i in range(self.envs.num_envs):
                #     episode_infos[i].append(infos[i])


                next_episodes = self.envs.current_episodes()
                envs_to_pause = []
                n_envs = self.envs.num_envs
                prev_obs = observations
                # for i in range(n_envs):

                #     if (
                #         next_episodes[i]['scene_id'],
                #         next_episodes[i]['episode_id'],
                #     ) in stats_episodes:
                #         envs_to_pause.append(i)

                #     # episode ended
                #     if not_done_masks[i].item() == 0:
                #         # pbar.update()
                #         episode_stats = dict()
                #         episode_stats["reward"] = current_episode_reward[i].item()
                #         episode_stats.update(
                #             self._extract_scalars_from_info(infos[i])
                #         )
                #         current_episode_reward[i] = 0


                #         # [!!] Add per-step episode information
                #         episode_info = []
                #         for info in episode_infos[i]:
                #             act_data = {'reward': info['reward'], 'action': info['action'], 'target': None, 'success': info['success']} 
                #             if 'target' in info:
                #                 act_data['target'] = info['target']['objectId']
                #             episode_info.append(act_data)
                #         episode_stats['step_info'] = episode_info
                #         episode_infos[i] = []

                #         # use scene_id + episode_id as unique id for storing stats
                #         stats_episodes[
                #             (
                #                 current_episodes[i]['scene_id'],
                #                 current_episodes[i]['episode_id'],
                #             )
                #         ] = episode_stats

                #         # [!!] Save episode data in the eval folder for processing
                #         scene, episode = current_episodes[i]['scene_id'], current_episodes[i]['episode_id']
                #         torch.save({'scene_id':scene,
                #                     'episode_id':episode,
                #                     'stats':episode_stats},
                #                     f'{self.config.CHECKPOINT_FOLDER}/eval/{scene}_{episode}.pth')
                        
                


                (
                    self.envs,
                    test_recurrent_hidden_states,
                    not_done_masks,
                    current_episode_reward,
                    prev_actions,
                    batch,
                    rgb_frames,
                ) = self._pause_envs(
                    envs_to_pause,
                    self.envs,
                    test_recurrent_hidden_states,
                    not_done_masks,
                    current_episode_reward,
                    prev_actions,
                    batch,
                    rgb_frames,
                )

            num_episodes = len(stats_episodes)
            aggregated_stats = dict()
            # for stat_key in next(iter(stats_episodes.values())).keys(): # [!!] Only output reward
            for stat_key in ['reward']:
                aggregated_stats[stat_key] = (
                    sum([v[stat_key] for v in stats_episodes.values()])
                    / num_episodes
                )

            for k, v in aggregated_stats.items():
                logger.info(f"Average episode {k}: {v:.4f}")

            self.envs.close()

    def no_action(self) -> None:
        r"""Main method of trainer evaluation. Calls _eval_checkpoint() that
        is specified in Trainer class that inherits from BaseRLTrainer
        Returns:
            None
        """

        os.makedirs(os.path.join(self.config.CHECKPOINT_FOLDER, 'eval/'), exist_ok=True)

        # add test episode information to config
        test_episodes = json.load(open(self.config.EVAL.DATASET))
        self.config.defrost()
        self.config.ENV.TEST_EPISODES = test_episodes
        self.config.ENV.TEST_EPISODE_COUNT = len(test_episodes)
        self.config.freeze()

        # Map location CPU is almost always better than mapping to a CUDA device.
        checkpoint_path = self.config.LOAD
        ckpt_dict = self.load_checkpoint(checkpoint_path, map_location="cpu")
        ppo_cfg = self.config.RL.PPO

        logger.info(f"env config: {self.config}")
        self.envs = construct_envs(self.config, get_env_class(self.config.ENV.ENV_NAME))
        self._setup_actor_critic_agent(ppo_cfg)

        # [!!] Log extra stuff
        logger.info(checkpoint_path)
        logger.info(f"num_steps: {self.config.ENV.NUM_STEPS}")

        # [!!] Only load if present
        if ckpt_dict is not None:
            self.agent.load_state_dict(ckpt_dict["state_dict"])
        else:
            logger.info('NO CHECKPOINT LOADED!')
        self.actor_critic = self.agent.actor_critic

        observations = self.envs.reset()
        batch = self.batch_obs(observations, self.device)

        current_episode_reward = torch.zeros(
            self.envs.num_envs, 1, device=self.device
        )

        test_recurrent_hidden_states = torch.zeros(
            self.actor_critic.net.num_recurrent_layers,
            self.config.NUM_PROCESSES,
            ppo_cfg.hidden_size,
            device=self.device,
        )
        prev_actions = torch.zeros(
            self.config.NUM_PROCESSES, 1, device=self.device, dtype=torch.long
        )
        not_done_masks = torch.zeros(
            self.config.NUM_PROCESSES, 1, device=self.device
        )
        stats_episodes = dict()  # dict of dicts that stores stats per episode

        rgb_frames = [
            [] for _ in range(self.config.NUM_PROCESSES)
        ]  # type: List[List[np.ndarray]]

        # [!!] Store extra information about the trajectory that the env does not return
        episode_infos = [[] for _ in range(self.config.NUM_PROCESSES)]

        pbar = tqdm.tqdm()
        self.actor_critic.eval()
        print("@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@")
        print(self.config.ENV.TEST_EPISODE_COUNT)
        print("@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@")
        action_list = []
        observation_list = []
        last_five_actions = deque(maxlen=5)
        while (
            len(stats_episodes) < self.config.ENV.TEST_EPISODE_COUNT
            and self.envs.num_envs > 0
        ):

            # [!!] Show more fine-grained progress. THOR is slow!
            pbar.update()
            
            current_episodes = self.envs.current_episodes()

            with torch.no_grad():
                (
                    _,
                    actions,
                    _,
                    test_recurrent_hidden_states,
                ) = self.actor_critic.act(
                    batch,
                    test_recurrent_hidden_states,
                    prev_actions,
                    not_done_masks,
                    deterministic=False,
                )

                prev_actions.copy_(actions)
            
            outputs = self.envs.step([a[0].item() for a in actions])
            observations, rewards, dones, infos = [
                list(x) for x in zip(*outputs)
            ]
            
            batch = self.batch_obs(observations, self.device)

            not_done_masks = torch.tensor(
                [[0.0] if done else [1.0] for done in dones],
                dtype=torch.float,
                device=self.device,
            )
            # print(infos[0]["action"],infos[0]["success"])
            last_five_actions.append([infos[0]["action"],infos[0]["success"]])

            # if len(last_five_actions) == 5 and all(x == last_five_actions[0] for x in last_five_actions):
            #     print("horiya")
            #     times = random.randint(1, 3)  # Randomly choose to call 1, 2, or 3 times
            #     for _ in range(times):
            #         result = self.envs.act("tright")
            #     self.envs.act("forward")
            #     self.envs.act("forward")
            #     self.envs.act("forward")
            #     self.envs.act("forward")                               

            # print(infos[0]['traj_masks'])
            # act_to_idx = {'forward': 0, 'up': 1, 'down': 2, 'tright': 3, 'tleft': 4, 'take': 5, 'put': 6, 'open': 7, 'close': 8, 'toggle-on': 9, 'toggle-off': 10, 'slice': 11}
            act_to_idx = {'forward': 0, 'up': 1, 'down': 2, 'tright': 3, 'tleft': 4, 'open': 5, 'close': 6}
            # print(type(observations[0]["rgb"]))
            # print(actions)
            # print(rewards)
            # print(infos[0])
            # print(dones)
            if not dones[0]:
                # print(actions.item())
                if rewards[0]== 0.9:
                    # if self.get_action(actions.item(),act_to_idx) == "take" or self.get_action(actions.item(),act_to_idx) == "put" or self.get_action(actions.item(),act_to_idx) == "open" or self.get_action(actions.item(),act_to_idx) == "close":
                    if self.get_action(actions.item(),act_to_idx) == "open" or self.get_action(actions.item(),act_to_idx) == "close":
                       # cv2.imwrite('{}_{}.png'.format(self.get_action(actions.item(),act_to_idx), '2'), observations[0]["rgb"])
                        # cv2.imwrite('{}_{}.png'.format(self.get_action(actions.item(),act_to_idx), '1'), prev_obs[0]["rgb"])
                        action_list.append(self.get_action(actions.item(),act_to_idx))
                        # observation_list.append([prev_obs[0],observations[0]])

                # if self.get_action(actions.item(),act_to_idx) == "take":
                #     actions = torch.tensor([[act_to_idx["put"]]])
                #     outputs = self.envs.step([a[0].item() for a in actions])
                #     observations, rewards, dones, infos = [
                #         list(x) for x in zip(*outputs)
                #     ]
                    
                #     batch = self.batch_obs(observations, self.device)

                #     not_done_masks = torch.tensor(
                #         [[0.0] if done else [1.0] for done in dones],
                #         dtype=torch.float,
                #         device=self.device,
                #     )

                #     action_list.append(self.get_action(actions.item(),act_to_idx))
                #     # observation_list.append([prev_obs[0],observations[0]])

                    # print(self.get_action(actions.item(),act_to_idx) )
                    if self.get_action(actions.item(),act_to_idx) == "open":
                        info = self.envs.act("take")
                        print(info)
                        info = self.envs.act("put")
                        print(info)

                        self.envs.act("up")

                        info = self.envs.act("take")
                        print(info)
                        info = self.envs.act("put")
                        print(info)

                        self.envs.act("down")
                        self.envs.act("down")

                        info = self.envs.act("take")
                        print(info)
                        info = self.envs.act("put")
                        print(info)

                        self.envs.act("up")
                        self.envs.act("tleft")

                        info = self.envs.act("take")
                        print(info)
                        info = self.envs.act("put")
                        print(info)

                        self.envs.act("tright")
                        self.envs.act("tright")
                        self.envs.act("down")

                        info = self.envs.act("take")
                        print(info)
                        info = self.envs.act("put")
                        print(info)

                        self.envs.act("up")
                        self.envs.act("tleft")
                        self.envs.act("close")
                                                       

                    outputs = self.envs.step([a[0].item() for a in actions])
                    observations, rewards, dones, infos = [
                        list(x) for x in zip(*outputs)
                    ]
                    
                    batch = self.batch_obs(observations, self.device)

                    not_done_masks = torch.tensor(
                        [[0.0] if done else [1.0] for done in dones],
                        dtype=torch.float,
                        device=self.device,
                    )

                    # action_list.append(self.get_action(actions.item(),act_to_idx))
                    # observation_list.append([prev_obs[0],observations[0]])





            if dones[0]:

                scene = current_episodes[0]['scene_id']
                episode = current_episodes[0]['episode_id']

                # Create filename
                filename = f"{scene}_{episode}.pkl"

                # Data to save
                data_to_save = {'action_list': action_list, 'observation_list': observation_list}  # Replace with your actual data

                # Save data to pickle file
                with open(filename, 'wb') as f:
                    pickle.dump(data_to_save, f)

                action_list = []
                observation_list = [] 



            rewards = torch.tensor(
                rewards, dtype=torch.float, device=self.device
            ).unsqueeze(1)

            current_episode_reward += rewards

            # # [!!] store epiode history
            # for i in range(self.envs.num_envs):
            #     episode_infos[i].append(infos[i])


            next_episodes = self.envs.current_episodes()
            envs_to_pause = []
            n_envs = self.envs.num_envs
            prev_obs = observations
            # for i in range(n_envs):

            #     if (
            #         next_episodes[i]['scene_id'],
            #         next_episodes[i]['episode_id'],
            #     ) in stats_episodes:
            #         envs_to_pause.append(i)

            #     # episode ended
            #     if not_done_masks[i].item() == 0:
            #         # pbar.update()
            #         episode_stats = dict()
            #         episode_stats["reward"] = current_episode_reward[i].item()
            #         episode_stats.update(
            #             self._extract_scalars_from_info(infos[i])
            #         )
            #         current_episode_reward[i] = 0


            #         # [!!] Add per-step episode information
            #         episode_info = []
            #         for info in episode_infos[i]:
            #             act_data = {'reward': info['reward'], 'action': info['action'], 'target': None, 'success': info['success']} 
            #             if 'target' in info:
            #                 act_data['target'] = info['target']['objectId']
            #             episode_info.append(act_data)
            #         episode_stats['step_info'] = episode_info
            #         episode_infos[i] = []

            #         # use scene_id + episode_id as unique id for storing stats
            #         stats_episodes[
            #             (
            #                 current_episodes[i]['scene_id'],
            #                 current_episodes[i]['episode_id'],
            #             )
            #         ] = episode_stats

            #         # [!!] Save episode data in the eval folder for processing
            #         scene, episode = current_episodes[i]['scene_id'], current_episodes[i]['episode_id']
            #         torch.save({'scene_id':scene,
            #                     'episode_id':episode,
            #                     'stats':episode_stats},
            #                     f'{self.config.CHECKPOINT_FOLDER}/eval/{scene}_{episode}.pth')
                    
            


            (
                self.envs,
                test_recurrent_hidden_states,
                not_done_masks,
                current_episode_reward,
                prev_actions,
                batch,
                rgb_frames,
            ) = self._pause_envs(
                envs_to_pause,
                self.envs,
                test_recurrent_hidden_states,
                not_done_masks,
                current_episode_reward,
                prev_actions,
                batch,
                rgb_frames,
            )

        num_episodes = len(stats_episodes)
        aggregated_stats = dict()
        # for stat_key in next(iter(stats_episodes.values())).keys(): # [!!] Only output reward
        for stat_key in ['reward']:
            aggregated_stats[stat_key] = (
                sum([v[stat_key] for v in stats_episodes.values()])
                / num_episodes
            )

        for k, v in aggregated_stats.items():
            logger.info(f"Average episode {k}: {v:.4f}")

        self.envs.close()
