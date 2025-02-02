import argparse
import time
from typing import Dict

from pettingzoo.mpe import simple_tag_v2

from malib.runner import start_logger, terminate_logger
from malib.utils.typing import (
    AgentID,
    EvaluateResult,
    RolloutFeedback,
    PolicyID,
    TrainingFeedback,
)


exp_cfg = {
    "expr_group": "single_instance_psro",
    "expr_name": f"simple_tag_{time.time()}",
}

start_logger(exp_cfg)

from malib.agent.indepdent_agent import IndependentAgent
from malib.rollout.rollout_worker import RolloutWorker
from malib.rollout.rollout_func import rollout_wrapper
from malib.evaluator.psro import PSROEvaluator
from malib.evaluator.utils.payoff_manager import PayoffManager

from malib.backend.datapool.offline_dataset_server import Episode
from malib.utils.formatter import pretty_print
from malib.utils.metrics import get_metric

parser = argparse.ArgumentParser(
    "Single instance of PSRO training on mpe environments."
)


parser.add_argument("--batch_size", type=int, default=64)
parser.add_argument("--num_epoch", type=int, default=1)
parser.add_argument("--fragment_length", type=int, default=25)
parser.add_argument("--worker_num", type=int, default=6)
parser.add_argument("--algorithm", type=str, default="PPO")
parser.add_argument("--max_iteration", type=int, default=20)
parser.add_argument("--buffer_size", type=int, default=100)
parser.add_argument("--rollout_metric", type=str, default="simple", choices={"simple"})


if __name__ == "__main__":
    args = parser.parse_args()
    env_config = {
        "num_good": 1,
        "num_adversaries": 1,
        "num_obstacles": 2,
        "max_cycles": 75,
    }

    env = simple_tag_v2.env(**env_config)
    possible_agents = env.possible_agents
    observation_spaces = env.observation_spaces
    action_spaces = env.action_spaces

    env_desc = {
        "creator": simple_tag_v2.env,
        "config": env_config,
        "id": "simple_tag_v2",
        "possible_agents": possible_agents,
    }

    # agent buffer, for sampling
    agent_episodes = {
        agent: Episode(env_desc["id"], policy_id=None, capacity=args.buffer_size)
        for agent in env.possible_agents
    }

    rollout_config = {
        "stopper": "simple_rollout",
        "metric_type": args.rollout_metric,
        "fragment_length": 100,
        "num_episodes": 1,
        "terminate": "any",
        "mode": "on_policy",
        "callback": rollout_wrapper(agent_episodes),  # online mode
    }

    algorithm = {"name": args.algorithm, "model_config": {}, "custom_config": {}}

    learners = {}
    for agent in env.possible_agents:
        learners[agent] = IndependentAgent(
            assign_id=agent,
            env_desc=env_desc,
            training_agent_mapping=None,
            algorithm_candidates={args.algorithm: algorithm},
            observation_spaces=observation_spaces,
            action_spaces=action_spaces,
            exp_cfg=exp_cfg,
        )
        learners[agent].register_env_agent(agent)

    rollout_handlers = {
        agent: RolloutWorker(
            worker_index=None,
            env_desc=env_desc,
            metric_type=args.rollout_metric,
            remote=False,
            exp_cfg=exp_cfg,
        )
        for agent in env.possible_agents
    }

    # evaluator for global convergence judgement
    psro_evaluator = PSROEvaluator(
        stop_metrics={
            PSROEvaluator.StopMetrics.MAX_ITERATION: args.max_iteration,
            PSROEvaluator.StopMetrics.PAYOFF_DIFF_THRESHOLD: 1e-2,
        }
    )
    # payoff manager, maintain agent payoffs and simulation status
    payoff_manager = PayoffManager(env.possible_agents, exp_cfg=exp_cfg)

    def run_simulation_and_update_payoff(
        pid_mapping: Dict[AgentID, PolicyID]
    ) -> Dict[AgentID, Dict[PolicyID, float]]:
        """ Run simulations and update payoff tables with returned RolloutFeedback """
        population = None
        for agent, rollout_handler in rollout_handlers.items():
            learner = learners[agent]
            pid = pid_mapping[agent]
            matches = payoff_manager.get_pending_matchups(
                agent,
                pid,  # PSRO requires only one policy for each agent
                learner.policies[pid].description,
            )
            statistics_list, _ = rollout_handler.sample(
                callback=rollout_config["callback"],
                behavior_policy_mapping=matches,
                num_episodes=10,
                threaded=False,
                role="simulation",
            )
            for statistics, match in zip(
                statistics_list, matches
            ):  # update payoff table
                payoff_manager.update_payoff(
                    RolloutFeedback(
                        worker_idx=None,
                        agent_involve_info=None,
                        policy_combination=match,
                        statistics=get_metric(
                            rollout_config["metric_type"]
                        ).merge_parsed(statistics),
                    )
                )
                population = rollout_handler.population

        # update population mapping with learned best response
        eq = payoff_manager.compute_equilibrium(population)
        payoff_manager.update_equilibrium(population, eq)
        return eq

    def extend_policy_pool(trainable=False) -> Dict[AgentID, PolicyID]:
        """ Extend policy pool for learner, rollout handlers and payoff manager """
        added_policy_mapping = {}
        for agent, learner in learners.items():
            pid, policy = learner.add_policy_for_agent(agent, trainable=trainable)
            # record policy information
            # register policy into rollout handlers
            _ = [
                rollout_handler.update_population(agent, pid, policy)
                for rollout_handler in rollout_handlers.values()
            ]
            added_policy_mapping[agent] = pid
        return added_policy_mapping

    def training_workflow(trainable_policy_mapping: Dict[AgentID, PolicyID]):
        """ Training workflow will run rollout first, then follow with training """
        rollout_feedback: Dict[AgentID, RolloutFeedback] = {}
        training_feedback: Dict[AgentID, TrainingFeedback] = {}
        population = list(rollout_handlers.values())[0].population
        # filter trainable policy
        for agent, policy in trainable_policy_mapping.items():
            temp = dict.fromkeys(population[agent], None)
            temp.pop(policy)
            population[agent] = list(temp.keys())
        for agent in env.possible_agents:
            # workflow for each agent
            policy_distribution = payoff_manager.get_equilibrium(population)
            policy_distribution[agent] = {trainable_policy_mapping[agent]: 1.0}
            # print(policy_distribution)
            rollout_handlers[agent].ready_for_sample(policy_distribution)
            for epoch in range(args.num_epoch):
                rollout_feedback[agent], _ = rollout_handlers[agent].sample(
                    callback=rollout_config["callback"],
                    num_episodes=[rollout_config["num_episodes"]],
                    threaded=False,
                    role="rollout",
                    trainable_pairs=trainable_policy_mapping,
                )
                batch = agent_episodes[agent].sample(size=args.batch_size)
                res = learners[agent].optimize(
                    policy_ids={agent: trainable_policy_mapping[agent]},
                    batch={agent: batch},
                    training_config={
                        "optimizer": "Adam",
                        "critic_lr": 1e-4,
                        "actor_lr": 1e-4,
                        "lr": 1e-4,
                        "update_interval": 5,
                        "cliprange": 0.2,
                        "entropy_coef": 0.001,
                        "value_coef": 0.5,
                    },
                )
                training_feedback.update(res)

        return {"rollout": rollout_feedback, "training": training_feedback}

    # init agent with fixed policy
    # XXX(ming): add fake trainable policy is not reasonable but resolve issue
    policy_mapping = extend_policy_pool(trainable=True)
    equilibrium: Dict[
        AgentID, Dict[PolicyID, float]
    ] = run_simulation_and_update_payoff(policy_mapping)

    # ============================================= Main Loop ========================================== #
    iteration = 0
    while True:
        # 1. add new trainable policy
        print(f"=========== Iteration #{iteration} ===========")
        trainable_policy_mapping = extend_policy_pool(trainable=True)
        # 2. do rollout and training workflow
        feedback: Dict = training_workflow(trainable_policy_mapping)
        # 3. simulation and payoff table update
        equilibrium: Dict[
            AgentID, Dict[PolicyID, float]
        ] = run_simulation_and_update_payoff(trainable_policy_mapping)
        print(f"------- Equilibrium:\n{pretty_print(equilibrium)}")
        # 4. judge converge
        nash_payoffs: Dict[AgentID, float] = payoff_manager.aggregate(
            equilibrium=equilibrium
        )
        weighted_payoffs: Dict[AgentID, float] = payoff_manager.aggregate(
            equilibrium=equilibrium,
            brs=trainable_policy_mapping,
        )
        evaluation_results = psro_evaluator.evaluate(
            None,
            weighted_payoffs=weighted_payoffs,
            oracle_payoffs=nash_payoffs,
            trainable_mapping=trainable_policy_mapping,
        )
        print(f"------- Evaluation:\n{pretty_print(evaluation_results)}\n")
        if (
            evaluation_results[EvaluateResult.CONVERGED]
            or evaluation_results[EvaluateResult.REACHED_MAX_ITERATION]
        ):
            print("converged!")
            break

        iteration += 1

    terminate_logger()
