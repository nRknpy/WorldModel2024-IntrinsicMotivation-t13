import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.distributions as td
from einops import rearrange

from .network import ExplorerActor, ExplorerCritic
from .worldmodel import WorldModel
from .explorer_reward import EmsembleReward
from .utils import compute_lambda_target


class Explorer(nn.Module):
    def __init__(self,
                 world_model: WorldModel,
                 action_dim,
                 z_dim,
                 num_classes,
                 h_dim,
                 mlp_hidden_dim,
                 min_std,
                 num_emsembles,
                 emsembles_offset,
                 emsembles_target_mode,
                 discount,
                 lambda_,
                 actor_entropy_scale,
                 device):
        super(Explorer, self).__init__()
        
        self.world_model = world_model
        
        self.action_dim = action_dim
        self.z_dim = z_dim
        self.num_classes = num_classes
        self.h_dim = h_dim
        self.mlp_hidden_dim = mlp_hidden_dim
        self.min_std = min_std
        self.num_emsembles = num_emsembles
        self.emsembles_offset = emsembles_offset
        self.emsembles_target_mode = emsembles_target_mode
        self.discount = discount
        self.lambda_ = lambda_
        self.actor_entropy_scale = actor_entropy_scale
        self.device = device
        
        self.actor = ExplorerActor(
            action_dim = action_dim,
            z_dim = z_dim,
            num_classes = num_classes,
            h_dim = h_dim,
            hidden_dim = mlp_hidden_dim,
            min_std = min_std
        )
        self.critic = ExplorerCritic(
            z_dim = z_dim,
            num_classes = num_classes,
            h_dim = h_dim,
            hidden_dim = mlp_hidden_dim
        )
        self.target_critic = ExplorerCritic(
            z_dim = z_dim,
            num_classes = num_classes,
            h_dim = h_dim,
            hidden_dim = mlp_hidden_dim
        )
        self.target_critic.load_state_dict(self.critic.state_dict())
        
        self.instrinsic_reward = EmsembleReward(
            z_dim = z_dim,
            num_classes = num_classes,
            h_dim = h_dim,
            min_std = min_std,
            mlp_hidden_dim = mlp_hidden_dim,
            device = device,
            num_emsembles = num_emsembles,
            offset = emsembles_offset,
            target_mode = emsembles_target_mode
        )
    
    def train(self, init_zs: torch.Tensor, init_hs: torch.Tensor, horison_length):
        zs = init_zs.detach() # (batch_size * seq_length, z_dim * num_classes)
        hs = init_hs.detach() # (batch_size * seq_length, h_dim)
        
        imagined_zs = torch.empty(horison_length, *init_zs.shape, device=self.device)
        imagined_hs = torch.empty(horison_length, *init_hs.shape, device=self.device)
        imagined_action_log_probs = torch.empty(horison_length, init_zs.shape[0], device=self.device)
        imagined_action_entropys = torch.empty(horison_length, init_zs.shape[0], device=self.device)
        
        for t in range(horison_length):
            actions, action_log_probs, action_entropys = self.actor(zs.detach(), hs.detach())
            
            with torch.no_grad():
                hs, zs = self.world_model.imagine(actions, zs, hs)
            
            imagined_hs[t] = hs.detach()
            imagined_zs[t] = zs.detach()
            imagined_action_log_probs[t] = action_log_probs
            imagined_action_entropys[t] = action_entropys
        
        flatten_hs = imagined_hs.view(-1, self.h_dim).detach() # (horison_length * batch_size * seq_length, h_dim)
        flatten_zs = imagined_zs.view(-1, self.z_dim * self.num_classes).detach() # (horison_length * batch_size * seq_length, z_dim * num_classes)
        
        with torch.no_grad():
            rewards = self.instrinsic_reward.compute_reward(flatten_zs, flatten_hs).view(horison_length, -1) # (horison_length, batch_size * seq_length)
            target_values = self.target_critic(flatten_zs, flatten_hs).view(horison_length, -1) # (horison_length, batch_size * seq_length)
        
        lambda_target = compute_lambda_target(rewards, self.discount, target_values, self.lambda_)
        
        objective = imagined_action_log_probs * ((lambda_target - target_values).detach())
        actor_loss = -torch.sum(torch.mean(objective + self.actor_entropy_scale * imagined_action_entropys, dim=1))
        
        value_mean = self.critic(flatten_zs.detach(), flatten_hs.detach()).view(horison_length, -1)
        value_dist = td.Independent(td.Normal(value_mean, 1),  1)
        critic_loss = -torch.mean(value_dist.log_prob(lambda_target.detach()).unsqueeze(-1))
        
        return actor_loss, critic_loss
    
    def update_critic(self):
        self.target_critic.load_state_dict(self.critic.state_dict())
