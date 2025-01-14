import gym
from gym import spaces
import numpy as np
from numpy import linalg as LA

from tf2marl.multiagent.multi_discrete import MultiDiscrete

# environment for all agents in the multiagent world
# currently code assumes that no agents will be created/destroyed at runtime!
class MultiAgentEnv(gym.Env):
    metadata = {
        'render.modes' : ['human', 'rgb_array']
    }

    def __init__(self, world, reset_callback=None, reward_callback=None,
                 observation_callback=None, info_callback=None,
                 done_callback=None, shared_viewer=True):

        self.world = world
        self.agents = self.world.policy_agents
        # set required vectorized gym env property
        self.n = len(world.policy_agents)
        self.n_adversaries = 0
        # scenario callbacks
        self.reset_callback = reset_callback
        self.reward_callback = reward_callback
        self.observation_callback = observation_callback
        self.info_callback = info_callback
        self.done_callback = done_callback
        # environment parameters
        self.discrete_action_space = True
        # if true, action is a number 0...N, otherwise action is a one-hot N-dimensional vector
        self.discrete_action_input = False
        # if true, even the action is continuous, action will be performed discretely
        self.force_discrete_action = world.discrete_action if hasattr(world, 'discrete_action') else False
        # if true, every agent has the same reward
        self.shared_reward = world.collaborative if hasattr(world, 'collaborative') else False
        self.time = 0

        # 報酬のリスト
        self.reward_list_all = []
        # configure spaces
        self.action_space = []
        self.observation_space = []
        for agent in self.agents:
            total_action_space = []
            # physical action space
            if self.discrete_action_space and not self.discrete_action_input:
                u_action_space = spaces.Discrete(world.dim_p * 2 + 1)
            elif self.discrete_action_space and self.discrete_action_input:
                u_action_space = spaces.Discrete(16)
            else:
                u_action_space = spaces.Box(low=-agent.u_range, high=+agent.u_range, shape=(world.dim_p,), dtype=np.float32)
            if agent.movable:
                total_action_space.append(u_action_space)
            # communication action space
            if self.discrete_action_space:
                c_action_space = spaces.Discrete(world.dim_c)
            else:
                c_action_space = spaces.Box(low=0.0, high=1.0, shape=(world.dim_c,), dtype=np.float32)
            if not agent.silent:
                total_action_space.append(c_action_space)
            # total action space
            if len(total_action_space) > 1:
                # all action spaces are discrete, so simplify to MultiDiscrete action space
                if all([isinstance(act_space, spaces.Discrete) for act_space in total_action_space]):
                    act_space = MultiDiscrete([[0, act_space.n - 1] for act_space in total_action_space])
                else:
                    act_space = spaces.Tuple(total_action_space)
                self.action_space.append(act_space)
            else:
                self.action_space.append(total_action_space[0])
            # observation space
            # obs_dim = len(observation_callback(agent, self.world))
            # self.observation_space.append(spaces.Box(low=-np.inf, high=+np.inf, shape=(obs_dim,), dtype=np.float32))
            obs_shape = observation_callback(agent, self.world).shape
            self.observation_space.append(spaces.Box(low=-np.inf, high=+np.inf, shape=obs_shape, dtype=np.float32))
            agent.action.c = np.zeros(self.world.dim_c)

        # rendering
        self.shared_viewer = shared_viewer
        if self.shared_viewer:
            self.viewers = [None]
        else:
            self.viewers = [None] * self.n
        self._reset_render()

    def step(self, action_n):
        obs_n = []
        reward_n = []
        done_n = []
        info_n = []
        self.agents = self.world.policy_agents
        # set action for each agent
        for i, agent in enumerate(self.agents):
            self._set_action(action_n[i], agent, self.action_space[i])
        # advance world state
        self.world.step()
        # record observation for each agent
        for i, agent in enumerate(self.agents):
            obs_n.append(self._get_obs(agent))
            # 報酬のリストを取得
            reward, reward_list = self._get_reward(agent)
            reward_n.append(reward)
            done, info = self._get_done(agent)  
            done_n.append(done)
            # 報酬可視化用
            self.reward_list_all[i].append(np.round(reward_list, decimals=2))

            # info_n['n'].append(self._get_info(agent))
            info_n.append(info)
        # all agents get total reward in cooperative case
        reward = np.sum(reward_n)
        if self.shared_reward:
            reward_n = [reward] * self.n

        return obs_n, reward_n, done_n, info_n

    def reset(self):
        # reset world
        self.dest, self.rho_g, self.__calc_F_COM = self.reset_callback(self.world)
        # reset renderer
        self._reset_render()
        # record observations for each agent
        obs_n = []
        self.reward_list_all = [[] for L in self.world.agents]
        self.agents = self.world.policy_agents
        for agent in self.agents:
            obs_n.append(self._get_obs(agent))
            
        self.world.num_episodes += 1
        return obs_n

    # get info used for benchmarking
    def _get_info(self, agent):
        if self.info_callback is None:
            return {}
        return self.info_callback(agent, self.world)

    # get observation for a particular agent
    def _get_obs(self, agent):
        if self.observation_callback is None:
            return np.zeros(0)
        return self.observation_callback(agent, self.world).astype(np.float32)

    # get dones for a particular agent
    # unused right now -- agents are allowed to go beyond the viewing screen
    def _get_done(self, agent):
        if self.done_callback is None:
            return False
        return self.done_callback(agent, self.world)

    # get reward for a particular agent
    def _get_reward(self, agent):
        if self.reward_callback is None:
            return 0.0
        return self.reward_callback(agent, self.world)

    # set env action for a particular agent
    def _set_action(self, action, agent, action_space, time=None):
        agent.action.u = np.zeros(self.world.dim_p)
        agent.action.c = np.zeros(self.world.dim_c)
        # process action
        if isinstance(action_space, MultiDiscrete):
            act = []
            size = action_space.high - action_space.low + 1
            index = 0
            for s in size:
                act.append(action[index:(index+s)])
                index += s
            action = act
        else:
            action = [action]

        if agent.movable:
            # physical action
            if self.discrete_action_input:
                agent.action.u = np.zeros(self.world.dim_p)
                d = np.argmax(action[0])
                # process discrete action
                if d == 0: agent.action.u[0] = 0; agent.action.u[1] = 1.0
                elif d == 1: agent.action.u[0] = 1.0; agent.action.u[1] = 1.0
                elif d == 2: agent.action.u[0] = 1.0; agent.action.u[1] = 0
                elif d == 3: agent.action.u[0] = 1.0; agent.action.u[1] = -1.0
                elif d == 4: agent.action.u[0] = 0; agent.action.u[1] = -1.0
                elif d == 5: agent.action.u[0] = -1.0; agent.action.u[1] = -1.0
                elif d == 6: agent.action.u[0] = -1.0; agent.action.u[1] = 0
                elif d == 7: agent.action.u[0] = -1.0; agent.action.u[1] = 1.0
                elif d == 8: agent.action.u[0] = 0; agent.action.u[1] = 0.5
                elif d == 9: agent.action.u[0] = 0.5; agent.action.u[1] = 0.5
                elif d == 10: agent.action.u[0] = 0.5; agent.action.u[1] = 0
                elif d == 11: agent.action.u[0] = 0.5; agent.action.u[1] = -0.5
                elif d == 12: agent.action.u[0] = 0; agent.action.u[1] = -0.5
                elif d == 13: agent.action.u[0] = -0.5; agent.action.u[1] = -0.5
                elif d == 14: agent.action.u[0] = -0.5; agent.action.u[1] = 0
                elif d == 15: agent.action.u[0] = -0.5; agent.action.u[1] = 0.5
            else:
                if self.force_discrete_action:
                    d = np.argmax(action[0])
                    action[0][:] = 0.0
                    action[0][d] = 1.0
                if self.discrete_action_space:
                    agent.action.u[0] += action[0][1] - action[0][2]
                    agent.action.u[1] += action[0][3] - action[0][4]
                else:
                    agent.action.u = action[0]
            sensitivity = 5.0
            if agent.accel is not None:
                sensitivity = agent.accel
            agent.action.u *= sensitivity
            action = action[1:]
        if not agent.silent:
            # communication action
            if self.discrete_action_input:
                agent.action.c = np.zeros(self.world.dim_c)
                agent.action.c[action[0]] = 1.0
            else:
                agent.action.c = action[0]
            action = action[1:]
        # make sure we used all elements of action
        assert len(action) == 0

    # reset rendering assets
    def _reset_render(self):
        self.render_geoms = None
        self.render_geoms_xform = None

    # render environment
    def render(self, mode='human'):
        if mode == 'human':
            alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
            message = ''
            for agent in self.world.agents:
                comm = []
                for other in self.world.agents:
                    if other is agent: continue
                    if np.all(other.state.c == 0):
                        word = '_'
                    else:
                        word = alphabet[np.argmax(other.state.c)]
                    message += (other.name + ' to ' + agent.name + ': ' + word + '   ')
            # print(message)

        for i in range(len(self.viewers)):
            # create viewers (if necessary)
            if self.viewers[i] is None:
                # import rendering only if we need it (and don't import for headless machines)
                #from gym.envs.classic_control import rendering
                from tf2marl.multiagent import rendering
                self.viewers[i] = rendering.Viewer(600, 600)

        # create rendering geometry
        # if self.render_geoms is None:
        # import rendering only if we need it (and don't import for headless machines)
        from tf2marl.multiagent import rendering
        self.render_geoms = []
        self.render_geoms_xform = []
        
        for entity in self.world.entities:
            geom = rendering.make_circle(entity.size)
            xform = rendering.Transform()
            if 'leader' in entity.name:
                geom.set_color(*entity.color, alpha=0.5)
            else:
                geom.set_color(*entity.color)
            geom.add_attr(xform)
            self.render_geoms.append(geom)
            self.render_geoms_xform.append(xform)
        
        # 目的地を追加
        geom_des = rendering.make_circle(self.rho_g)
        geom_des.set_color(*np.array([0.5, 0.5, 0.5]), alpha=0.5) # alphaは透明度を表す
        xform_des = rendering.Transform()
        geom_des.add_attr(xform_des)
        self.render_geoms.append(geom_des)
        self.render_geoms_xform.append(xform_des)
        # 重心を追加
        size = 0.04
        points = [(size, 0), (0, 1.5 * size), (-size, 0), (0, -1.5 * size)]
        geom_COM = rendering.make_polygon(points)
        geom_COM.set_color(*np.array([0, 0.5, 0]), alpha=1) # alphaは透明度を表す
        xform_COM = rendering.Transform()
        geom_COM.add_attr(xform_COM)
        self.render_geoms.append(geom_COM)
        self.render_geoms_xform.append(xform_COM)
        # followerの外接矩形を追加
        if len(self.world.box) != 0: 
            center = np.mean(self.world.box, axis = 0)
            geom_rect = rendering.make_polygon(self.world.box - center, filled=False)
            xform_rect = rendering.Transform()
            geom_rect.set_color(*np.array([0, 0, 1]))
            geom_rect.add_attr(xform_rect)
            self.render_geoms.append(geom_rect)
            self.render_geoms_xform.append(xform_rect)
        # 障害物周りのmax_rangeを追加
        for O in self.world.obstacles:
            if O.have_vel:
                geom_obs = rendering.make_circle(O.max_range)
                xform_obs = rendering.Transform()
                geom_obs.set_color(*np.array([0, 0.5, 0]), alpha=0.1)
                geom_obs.add_attr(xform_obs)
                self.render_geoms.append(geom_obs)
                self.render_geoms_xform.append(xform_obs)
                
        # add geoms to viewer
        for viewer in self.viewers:
            viewer.geoms = []
            for geom in self.render_geoms:
                viewer.add_geom(geom)

        results = []; 
        for i in range(len(self.viewers)):
            # update bounds to center around agent
            cam_range = 10 # 拡大、縮小を決める変数
            if self.shared_viewer:
                # pos = np.zeros(self.world.dim_p)
                pos = self.dest / 2
            else:
                pos = self.agents[i].state.p_pos
            self.viewers[i].set_bounds(pos[0] - cam_range,pos[0] + cam_range, pos[1]-cam_range, pos[1]+ cam_range)
            # update geometry positions
            for e, entity in enumerate(self.world.entities):
                self.render_geoms_xform[e].set_translation(*entity.state.p_pos)
            # 目標値の更新
            self.render_geoms_xform[len(self.world.entities)].set_translation(*self.dest)
            # 重心の更新
            follower_COM = self.__calc_F_COM(self.world)
            self.render_geoms_xform[len(self.world.entities) + 1].set_translation(*follower_COM)
            # 外接矩形の更新
            if len(self.world.box) != 0:
                self.render_geoms_xform[len(self.world.entities) + 2].set_translation(*center)
            # 障害物周りのmax_rangeの更新
            count = 0
            for O in self.world.obstacles:
                if O.have_vel:
                    count += 1
                    pos = O.init_pos
                    self.render_geoms_xform[len(self.world.entities) + 2 + count].set_translation(*pos)
            # render to display or array
            results.append(self.viewers[i].render(return_rgb_array = mode =='rgb_array'))
        return results

    # create receptor field locations in local coordinate frame
    def _make_receptor_locations(self, agent):
        receptor_type = 'polar'
        range_min = 0.05 * 2.0
        range_max = 1.00
        dx = []
        # circular receptive field
        if receptor_type == 'polar':
            for angle in np.linspace(-np.pi, +np.pi, 8, endpoint=False):
                for distance in np.linspace(range_min, range_max, 3):
                    dx.append(distance * np.array([np.cos(angle), np.sin(angle)]))
            # add origin
            dx.append(np.array([0.0, 0.0]))
        # grid receptive field
        if receptor_type == 'grid':
            for x in np.linspace(-range_max, +range_max, 5):
                for y in np.linspace(-range_max, +range_max, 5):
                    dx.append(np.array([x,y]))
        return dx


# vectorized wrapper for a batch of multi-agent environments
# assumes all environments have the same observation and action space
class BatchMultiAgentEnv(gym.Env):
    metadata = {
        'runtime.vectorized': True,
        'render.modes' : ['human', 'rgb_array']
    }

    def __init__(self, env_batch):
        self.env_batch = env_batch

    @property
    def n(self):
        return np.sum([env.n for env in self.env_batch])

    @property
    def action_space(self):
        return self.env_batch[0].action_space

    @property
    def observation_space(self):
        return self.env_batch[0].observation_space

    def step(self, action_n, time):
        obs_n = []
        reward_n = []
        done_n = []
        info_n = {'n': []}
        i = 0
        for env in self.env_batch:
            obs, reward, done, _ = env.step(action_n[i:(i+env.n)], time)
            i += env.n
            obs_n += obs
            # reward = [r / len(self.env_batch) for r in reward]
            reward_n += reward
            done_n += done
        return obs_n, reward_n, done_n, info_n

    def reset(self):
        obs_n = []
        for env in self.env_batch:
            obs_n += env.reset()
        return obs_n

    # render environment
    def render(self, mode='human', close=True):
        results_n = []
        for env in self.env_batch:
            results_n += env.render(mode, close)
        return results_n
