import os
os.environ['OMP_NUM_THREADS'] = '1'
os.environ['THEANO_FLAGS'] = 'device=cpu'

import argparse
import numpy as np
from model import build_model, Agent
from time import sleep
from multiprocessing import Process, cpu_count, Value, Queue
import Queue as queue
from memory import ReplayMemory
from agent import run_agent, elu, sigmoid
from state import StateVelCentr, StateVel
import lasagne
import random
from environments import RunEnv2
from datetime import datetime
from time import time
from collections import deque


def get_args():
    parser = argparse.ArgumentParser(description="Run commands")
    parser.add_argument('--gamma', type=float, default=0.995, help="Discount factor for reward.")
    parser.add_argument('--num_agents', type=int, default=cpu_count()-1, help="Number of agents to run.")
    parser.add_argument('--sleep', type=int, default=0, help="Sleep time in seconds before start each worker.")
    parser.add_argument('--max_steps', type=int, default=10000000, help="Number of steps.")
    parser.add_argument('--test_period_min', default=30, type=int, help="Test interval int min.")
    parser.add_argument('--save_period_min', default=30, type=int, help="Save interval int min.")
    parser.add_argument('--num_test_episodes', type=int, default=5, help="Number of test episodes.")
    parser.add_argument('--batch_size', type=int, default=2000, help="Batch size.")
    parser.add_argument('--start_train_steps', type=int, default=10000, help="Number of steps tp start training.")
    parser.add_argument('--critic_lr', type=float, default=2e-3, help="critic learning rate")
    parser.add_argument('--actor_lr', type=float, default=1e-3, help="actor learning rate.")
    parser.add_argument('--critic_lr_end', type=float, default=5e-5, help="critic learning rate")
    parser.add_argument('--actor_lr_end', type=float, default=5e-5, help="actor learning rate.")
    parser.add_argument('--flip_prob', type=float, default=0., help="Probability of flipping.")
    parser.add_argument('--layer_norm', action='store_true', help="Use layer normaliation.")
    parser.add_argument('--exp_name', type=str, default=datetime.now().strftime("%d.%m.%Y-%H:%M"),
                        help='Experiment name')
    parser.add_argument('--last_n_states', type=int, default=8, help="Number of last states to feed in rnn.")
    return parser.parse_args()


def test_agent(testing, state_transform, last_n_states, num_test_episodes,
               model_params, weights, best_reward, updates, save_dir):
    testing.value = 1
    env = RunEnv2(state_transform, max_obstacles=3, skip_frame=5, last_n_states=last_n_states)
    test_rewards = []

    train_fn, actor_fn, target_update_fn, params_actor, params_crit, actor_lr, critic_lr = \
        build_model(**model_params)
    actor = Agent(actor_fn, params_actor, params_crit)
    actor.set_actor_weights(weights)

    action_deque = deque(maxlen=env.last_n_states)

    for ep in range(num_test_episodes):
        seed = random.randrange(2**32-2)
        state = env.reset(seed=seed, difficulty=2)
        test_reward = 0
        for _ in range(last_n_states):
            action_deque.append(np.zeros(18, dtype='float32'))

        while True:
            action_seq = np.stack(action_deque)
            _state = np.concatenate([state, action_seq], axis=1).astype('float32')

            action = actor.act(_state)
            action_deque.append(action)

            state, reward, terminal, _ = env.step(action)
            test_reward += reward
            if terminal:
                break

        test_rewards.append(test_reward)
    mean_reward = np.mean(test_rewards)
    std_reward = np.std(test_rewards)
    print 'test reward mean: {:.2f}, std: {:.2f}, all: {} '.\
        format(float(mean_reward), float(std_reward), test_rewards)

    if mean_reward > best_reward.value or mean_reward > 30 * env.reward_mult:
        if mean_reward > best_reward.value:
            best_reward.value = mean_reward
        fname = os.path.join(save_dir, 'weights_updates_{}_reward_{:.2f}.pkl'.
                             format(updates.value, mean_reward))
        actor.save(fname)
    testing.value = 0


def make_rnn_state(states, actions, n):
    n_samples, num_features = states.shape
    n_samples, num_actions = actions.shape

    first_actions = np.zeros((n, num_actions), dtype='float32')
    actions = np.concatenate([first_actions, actions[:-1]], axis=0)

    first_states = np.tile(states[0], n-1).reshape(-1, num_features)
    states = np.concatenate([first_states, states])

    states = np.concatenate([states, actions], axis=1)

    new_states = []
    for i in range(n_samples):
        new_states.append(states[i:i+n])
    return np.asarray(new_states)


def main():
    args = get_args()

    # create save directory
    save_dir = os.path.join('weights', args.exp_name)
    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    #state_transform = StateVelCentr(exclude_obstacles=True)
    state_transform = StateVelCentr(obstacles_mode='standard',
                                    exclude_centr=True,
                                    vel_states=[])
    #state_transform = StateVel(exclude_obstacles=True)
    num_actions = 18

    state_shape = (args.last_n_states, state_transform.state_size + num_actions)

    # build model
    model_params = {
        'state_shape': state_shape,
        'num_act': num_actions,
        'gamma': args.gamma,
        'actor_lr': args.actor_lr,
        'critic_lr': args.critic_lr,
        'layer_norm': args.layer_norm
    }
    train_fn, actor_fn, target_update_fn, params_actor, params_crit, actor_lr, critic_lr = \
        build_model(**model_params)
    actor = Agent(actor_fn, params_actor, params_crit)

    actor_lr_step = (args.actor_lr - args.actor_lr_end) / args.max_steps
    critic_lr_step = (args.critic_lr - args.critic_lr_end) / args.max_steps

    # build actor
    weights = [p.get_value() for p in params_actor]

    # build replay memory
    memory = ReplayMemory(state_shape, 18, 5000000)

    # init shared variables
    global_step = Value('i', 0)
    updates = Value('i', 0)
    best_reward = Value('f', -1e8)
    testing = Value('i', 0)

    # init agents
    data_queue = Queue()
    workers = []
    weights_queues = []
    for i in xrange(args.num_agents):
        w_queue = Queue()
        worker = Process(target=run_agent,
                         args=(model_params, weights, state_transform, args.last_n_states,
                               data_queue, w_queue, i, global_step, updates, best_reward,
                               args.max_steps)
                         )
        worker.daemon = True
        worker.start()
        sleep(args.sleep)
        workers.append(worker)
        weights_queues.append(w_queue)

    prev_steps = 0
    start_save = time()
    start_test = time()
    while global_step.value < args.max_steps:

        # get all data
        try:
            i, (states, actions, rewards, terminals) = data_queue.get_nowait()
            weights_queues[i].put(weights)
            # add data to memory
            states = make_rnn_state(states, actions, args.last_n_states)
            memory.add_samples(states, actions, rewards, terminals)
        except queue.Empty:
            pass

        # training step
        # TODO: consider not training during testing model
        if len(memory) > args.start_train_steps:
            batch = memory.random_batch(args.batch_size)

            if np.random.rand() < args.flip_prob:
                states, actions, rewards, terminals, next_states = batch

                states_flip = state_transform.flip_states(states)
                left = states_flip[..., -18:-9].copy()
                right = states_flip[..., -9:].copy()
                states_flip[..., -18:-9] = right
                states_flip[..., -9:] = left

                next_states_flip = state_transform.flip_states(next_states)
                left = next_states_flip[..., -18:-9].copy()
                right = next_states_flip[..., -9:].copy()
                next_states_flip[..., -18:-9] = right
                next_states_flip[..., -9:] = left

                actions_flip = np.zeros_like(actions)
                actions_flip[:, :num_actions//2] = actions[:, num_actions//2:]
                actions_flip[:, num_actions//2:] = actions[:, :num_actions//2]

                states_all = np.concatenate((states, states_flip))
                actions_all = np.concatenate((actions, actions_flip))
                rewards_all = np.tile(rewards.ravel(), 2).reshape(-1, 1)
                terminals_all = np.tile(terminals.ravel(), 2).reshape(-1, 1)
                next_states_all = np.concatenate((next_states, next_states_flip))
                batch = (states_all, actions_all, rewards_all, terminals_all, next_states_all)

            actor_loss, critic_loss = train_fn(*batch)
            updates.value += 1
            if np.isnan(actor_loss):
                raise Value('actor loss is nan')
            if np.isnan(critic_loss):
                raise Value('critic loss is nan')
            target_update_fn()
            weights = actor.get_actor_weights()

        delta_steps = global_step.value - prev_steps
        prev_steps += delta_steps

        actor_lr.set_value(lasagne.utils.floatX(max(actor_lr.get_value() - delta_steps*actor_lr_step, args.actor_lr_end)))
        critic_lr.set_value(lasagne.utils.floatX(max(critic_lr.get_value() - delta_steps*critic_lr_step, args.critic_lr_end)))

        # check if need to save and test
        if (time() - start_save)/60. > args.save_period_min:
            fname = os.path.join(save_dir, 'weights_updates_{}.pkl'.format(updates.value))
            actor.save(fname)
            start_save = time()

        # start new test process
        if (time() - start_test) / 60. > args.test_period_min and testing.value == 0:
            worker = Process(target=test_agent,
                             args=(testing, state_transform, args.last_n_states,
                                   args.num_test_episodes, model_params, weights,
                                   best_reward, updates, save_dir)
                             )
            worker.daemon = True
            worker.start()
            start_test = time()

    # end all processes
    for w in workers:
        w.join()


if __name__ == '__main__':
    main()
