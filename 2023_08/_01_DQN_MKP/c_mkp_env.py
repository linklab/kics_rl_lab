import enum
import random
from datetime import datetime
from typing import Optional, List, Final, Tuple, Union

from gymnasium import spaces
import gymnasium as gym
import numpy as np
from gymnasium.core import RenderFrame

from a_config import STATIC_RESOURCE_DEMAND_SAMPLE, STATIC_VALUE_SAMPLE


class DoneReasonType(enum.Enum):
    TYPE_FAIL_1 = "The Same Item Selected"
    TYPE_FAIL_2 = "Resource Limit Exceeded"
    TYPE_SUCCESS_1 = "All Items Allocated Successfully"
    TYPE_SUCCESS_2 = "An Unavailable Resource"


class MkpEnv(gym.Env):
    def __init__(self, env_config, verbose=False):
        super(MkpEnv, self).__init__()

        random.seed(datetime.now().timestamp())

        self._internal_state: Optional[np.ndarray] = None
        self._actions_selected: Optional[List[int]] = None

        self._n_resources: Final[int] = env_config["num_resources"]

        self._value_allocated = 0
        self._resources_allocated = [0] * self._n_resources

        self._total_value: Optional[int] = None
        self._reward: Optional[float] = None

        self._action_mask: Optional[np.ndarray] = None

        self._num_items: Final[int] = env_config["num_items"]
        self._initial_resources_capacity: Final[List[int]] = env_config["initial_resources_capacity"]
        self._lowest_item_resource_demand: Final[List[int]] = env_config["lowest_item_resource_demand"]
        self._highest_item_resource_demand: Final[List[int]] = env_config["highest_item_resource_demand"]
        self._lowest_item_value: Final[int] = env_config["lowest_item_value"]
        self._highest_item_value: Final[int] = env_config["highest_item_value"]
        self._use_static_item_resource_demand: Final[bool] = env_config["use_static_item_resource_demand"]
        self._use_same_item_resource_demand: Final[bool] = env_config["use_same_item_resource_demand"]
        self._state_normalization: Final[bool] = env_config["state_normalization"]

        max_state_value = max(self._highest_item_resource_demand + self._initial_resources_capacity)
        max_state_value = max(max_state_value, self._highest_item_value)

        self._action_space = spaces.Discrete(n=self._num_items)
        self._observation_space = spaces.Box(
            low=0.0,
            high=max_state_value,
            shape=((self._num_items + 1) * (2+self._n_resources),)
        )

        self._item_resource_demand: Optional[List[List[int]]] = None
        self._item_values: Optional[List[int]] = None

        if verbose:
            self._print_env_config(env_config)

    @property
    def num_items(self):
        return self._num_items

    @property
    def n_resources(self):
        return self._n_resources

    @property
    def initial_resources_capacity(self):
        return self._initial_resources_capacity

    @property
    def action_space(self):
        return self._action_space

    @property
    def observation_space(self):
        return self._observation_space

    @property
    def item_resource_demand(self):
        return self._item_resource_demand

    @property
    def item_values(self):
        return self._item_values

    def get_initial_internal_state(self) -> np.ndarray:
        state = np.zeros(shape=(self._num_items + 1, 2+self._n_resources), dtype=float)

        if self._use_static_item_resource_demand:
            self._item_resource_demand = STATIC_RESOURCE_DEMAND_SAMPLE
            self._item_values = STATIC_VALUE_SAMPLE
        else:
            if self._use_same_item_resource_demand:
                if self._item_resource_demand is None:
                    self._item_resource_demand = np.zeros(shape=(self._num_items, self._n_resources))
                    self._item_values = np.zeros(shape=(self._num_items,))
                    for item_idx in range(self._num_items):
                        self._item_resource_demand[item_idx] = np.random.randint(
                            low=self._lowest_item_resource_demand,
                            high=self._highest_item_resource_demand,
                            size=(self._n_resources,)
                        )
                        self._item_values[item_idx] = np.random.randint(
                            low=self._lowest_item_value,
                            high=self._highest_item_value,
                            size=(1,)
                        )
            else:
                self._item_resource_demand = np.zeros(shape=(self._num_items, self._n_resources))
                self._item_values = np.zeros(shape=(self._num_items,))
                for item_idx in range(self._num_items):
                    self._item_resource_demand[item_idx] = np.random.randint(
                        low=self._lowest_item_resource_demand,
                        high=self._highest_item_resource_demand,
                        size=(self._n_resources,)
                    )
                    self._item_values[item_idx] = np.random.randint(
                        low=self._lowest_item_value,
                        high=self._highest_item_value,
                        size=(1,)
                    )

        state[:-1, 1] = self._item_values
        state[:-1, 2:] = self._item_resource_demand
        state[-1, 2:] = np.array(self._initial_resources_capacity)

        self._total_value = state[:-1, 1].sum()

        return state

    def reset(self, **kwargs) -> Tuple[np.ndarray, dict]:
        self._internal_state = self.get_initial_internal_state()
        self._actions_selected = []
        self._value_allocated = 0
        self._resources_allocated = [0] * self._n_resources
        self._action_mask = np.zeros(shape=(self._num_items,), dtype=float)

        # compute action  mask
        unavailable_items_indices = self._get_unavailable_items_indices()
        self._action_mask[unavailable_items_indices] = 1.0

        # make observation
        state = self._internal_state
        if self._state_normalization:
            state = self._normalize_internal_state(state)
        obs = state.flatten()

        # make info
        info = {}
        self.fill_info(info)
        info["ACTION_MASK"] = self._action_mask

        observation = obs

        if len(unavailable_items_indices) == self._num_items:
            observation, info = self.reset(**kwargs)

        return observation, info

    def step(self, action_idx: int):
        self._actions_selected.append(action_idx)

        value_step = self._internal_state[action_idx, 1]
        resources_step = [-1] * self._n_resources
        for i in range(self._n_resources):
            resources_step[i] = self._internal_state[action_idx, i+2]

        self._reward = self.compute_reward(value_step)

        assert (self._internal_state[action_idx, 0] == 0), "The Same Item Selected: {0}".format(action_idx)
        assert all([
            self._resources_allocated[i] + resources_step[i] <= self._initial_resources_capacity[i]
            for i in range(self._n_resources)
        ]), f"{self._resources_allocated} + {resources_step} <= {self._initial_resources_capacity}"

        self._internal_state[action_idx, 0] = 1.0
        self._internal_state[action_idx, 1:] = 0.0

        self._internal_state[-1, 1] = self._internal_state[-1, 1] + value_step
        self._internal_state[-1, 2:] -= resources_step

        self._value_allocated += value_step
        self._resources_allocated += resources_step

        # >>> Make Transition >>>
        # Make next_observation
        state = self._internal_state
        if self._state_normalization:
            state = self._normalize_internal_state(state)
        next_obs = state.flatten()

        # Make reward
        reward = self.get_reward()

        # Make terminated, info
        info = {}
        unavailable_items_indices = self._get_unavailable_items_indices()

        if len(unavailable_items_indices) == self._num_items:
            terminated = True
            info['DoneReasonType'] = DoneReasonType.TYPE_SUCCESS_2
            info["ACTION_MASK"] = np.ones(shape=(self._num_items,), dtype=float) * -1.0
        else:
            terminated = False
            self._action_mask[unavailable_items_indices] = 1.0
            info["ACTION_MASK"] = self._action_mask
        self.fill_info(info)

        # Make truncated
        truncated = False
        # <<< Make Transition <<<

        next_observation = next_obs

        return next_observation, reward, terminated, truncated, info

    def render(self) -> Optional[Union[RenderFrame, List[RenderFrame]]]:
        return None

    def get_reward(self):
        return self._reward

    def compute_reward(self, value_step: int) -> float:
        reward = value_step / self._total_value
        assert reward < 1.0
        return reward

    def fill_info(self, info: dict):
        info["INTERNAL_STATE"] = self._internal_state
        info["ACTIONS_SELECTED"] = self._actions_selected
        info["VALUE_ALLOCATED"] = self._value_allocated
        info["RESOURCES_ALLOCATED"] = self._resources_allocated
        info["TOTAL_ALLOCATED"] = np.sum(self._resources_allocated)
        info["INITIAL_RESOURCES_CAPACITY"] = self._initial_resources_capacity,
        info["EACH_RESOURCE_DEMAND"] = np.sum(self._internal_state[:-1, 2:], axis=0)

    def _get_unavailable_items_indices(self):
        allocated_items = self._internal_state[:-1, 0] == 1.0
        lacking_resources_items = np.array([False] * self._num_items)
        for i in range(self._n_resources):
            resource_demand_nparray = self._internal_state[:-1, 2 + i]
            available_resources = self._internal_state[-1, 2 + i]
            lacking_resources_items = lacking_resources_items | (resource_demand_nparray > available_resources)

        unavailable_items_indices = np.where(
            allocated_items | lacking_resources_items
        )[0]

        return unavailable_items_indices

    def _normalize_internal_state(self, internal_state):
        normalized_state = internal_state.copy()

        normalized_state[:, 1] = internal_state[:, 1] / self._highest_item_value
        for i in range(self._n_resources):
            normalized_state[:, i+2] = internal_state[:, i+2] / self._initial_resources_capacity[i]

        return normalized_state

    def _print_env_config(self, env_config):
        print("{0:>50}: {1}".format("NUM_ITEMS", self._num_items))
        print("{0:>50}: {1}".format("LOWEST_ITEM_VALUE", env_config["lowest_item_value"]))
        print("{0:>50}: {1}".format("HIGHEST_ITEM_VALUE", env_config["highest_item_value"]))
        print("{0:>50}: {1}".format("LOWEST_ITEM_RESOURCE_DEMAND", env_config["lowest_item_resource_demand"]))
        print("{0:>50}: {1}".format("HIGHEST_ITEM_RESOURCE_DEMAND", env_config["highest_item_resource_demand"]))
        print("{0:>50}: {1}".format("INITIAL_RESOURCES_CAPACITY", env_config["initial_resources_capacity"]))
        print("{0:>50}: {1}".format("USE_STATIC_ITEM_RESOURCE_DEMAND", env_config["use_static_item_resource_demand"]))
        print("{0:>50}: {1}".format("USE_SAME_ITEM_RESOURCE_DEMAND", env_config["use_same_item_resource_demand"]))
