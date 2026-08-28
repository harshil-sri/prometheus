"""
rl_agent.py — Reinforcement Learning Red Team Agent

Uses a lightweight Policy Gradient approach (or deep Q-learning conceptually)
to learn how to evade the Blue Team ensemble by mutating attack parameters.
"""

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from collections import deque
import random

class AttackMutatorNet(nn.Module):
    def __init__(self, state_dim, action_dim):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(state_dim, 32),
            nn.ReLU(),
            nn.Linear(32, 32),
            nn.ReLU(),
            nn.Linear(32, action_dim),
            nn.Softmax(dim=-1)
        )
        
    def forward(self, x):
        return self.net(x)

class RLAgent:
    """
    Reinforcement Learning agent for adversarial attack mutation.
    Learns to pick mutation actions that lower the defense ensemble's fraud probability.
    """
    def __init__(self, state_dim=5, action_dim=4, lr=0.01, gamma=0.99, seed=42):
        self.state_dim = state_dim
        self.action_dim = action_dim
        self.gamma = gamma
        safe_seed = int(seed) % (2 ** 31)  # numpy uint32 guard
        torch.manual_seed(safe_seed)
        np.random.seed(safe_seed)
        random.seed(safe_seed)
        
        self.policy_net = AttackMutatorNet(state_dim, action_dim)
        self.optimizer = optim.Adam(self.policy_net.parameters(), lr=lr)
        
        # Memory for REINFORCE (Policy Gradient)
        self.saved_log_probs = []
        self.rewards = []
        
        # Action meanings (categorical mutators for simplicity)
        # 0: Decrease amount by 20%
        # 1: Increase amount by 20%
        # 2: Increase hop count / camouflage
        # 3: Delay timing (decrease velocity)
        
    def select_action(self, state):
        state = torch.from_numpy(np.array(state)).float().unsqueeze(0)
        probs = self.policy_net(state)
        m = torch.distributions.Categorical(probs)
        action = m.sample()
        self.saved_log_probs.append(m.log_prob(action))
        return action.item()
        
    def store_reward(self, reward):
        self.rewards.append(reward)
        
    def update_policy(self):
        """Perform one policy gradient update step."""
        if not self.rewards:
            return 0.0
            
        R = 0
        policy_loss = []
        returns = []
        for r in self.rewards[::-1]:
            R = r + self.gamma * R
            returns.insert(0, R)
            
        returns = torch.tensor(returns)
        if len(returns) > 1:
            returns = (returns - returns.mean()) / (returns.std() + 1e-9)
            
        for log_prob, R in zip(self.saved_log_probs, returns):
            policy_loss.append(-log_prob * R)
            
        self.optimizer.zero_grad()
        loss = torch.cat(policy_loss).sum()
        loss.backward()
        self.optimizer.step()
        
        # Clear memory
        del self.rewards[:]
        del self.saved_log_probs[:]
        
        return loss.item()

    def mutate_spec(self, spec, action):
        """Mutate the attack specification based on the chosen action."""
        import copy
        new_spec = copy.deepcopy(spec)
        
        # Action 0: Decrease amount
        if action == 0 and "amount" in new_spec:
            if isinstance(new_spec["amount"], (int, float)):
                new_spec["amount"] = max(1.0, new_spec["amount"] * 0.8)
            elif isinstance(new_spec["amount"], list):
                new_spec["amount"] = [max(1.0, a * 0.8) for a in new_spec["amount"]]
                
        # Action 1: Increase amount
        elif action == 1 and "amount" in new_spec:
            if isinstance(new_spec["amount"], (int, float)):
                new_spec["amount"] = new_spec["amount"] * 1.2
            elif isinstance(new_spec["amount"], list):
                new_spec["amount"] = [a * 1.2 for a in new_spec["amount"]]
                
        # Action 2: Increase hop count / camouflage
        elif action == 2:
            cam = new_spec.get("desired_camouflage", "medium")
            new_spec["desired_camouflage"] = "high" if cam in ["low", "medium"] else "very_high"
            
        # Action 3: Decrease velocity (delay)
        elif action == 3 and "variant_params" in new_spec:
            vp = new_spec["variant_params"]
            if "velocity" in vp:
                vp["velocity"] = max(1, int(vp["velocity"] * 0.5))
                
        return new_spec
