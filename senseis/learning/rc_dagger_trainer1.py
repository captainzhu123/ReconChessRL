from os import path
import torch
from torch import nn
from torch.utils import data
from torch.optim import Adam

from .rc_trainer import RCSelfTrainer

from senseis.collectors.rc_sense_eb2 import RCSenseEpisode2, RCSenseEC2, RCSenseEB2
from senseis.collectors.rc_action_eb3 import RCActionEpisode3, RCActionEC3, RCActionEB3
from senseis.encoders.rc_encoder5 import RCStateEncoder5
from senseis.encoders.rc_encoder5 import RCSenseEncoder3
from senseis.encoders.rc_encoder5 import RCActionEncoder4
from senseis.models.rc_sense_model2 import RCSenseModel2
from senseis.models.rc_action_model4 import RCActionModel4
from senseis.rewards.rc_sense_reward import rc_sense_reward2
from senseis.agents.rc_dagger_agent2 import RCDaggerAgent2
from senseis.torch_modules.loss import PGError
from senseis.learning.rc_qconfig import DaggerConfig

class RCDaggerTrainer1(RCSelfTrainer):
  def __init__(self, config: DaggerConfig, reporter):
    self.config = config
    self.sense_ec = RCSenseEC2()
    self.action_ec= RCActionEC3()
    self.sense_episode_exp = []
    self.action_episode_exp = []
    self.sense_model = None
    self.action_model = None
    self.snapshot_count = None
    self.reporter = reporter

  def episodes(self):
    return self.config.episodes

  def initialize(self):
    #do nothing
    pass

  def teardown(self):
    for exp in self.sense_episode_exp:
      self.sense_ec.append_episode(exp)
    for exp in self.action_episode_exp:
      self.action_ec.append_episode(exp)
    self.sense_episode_exp = []
    self.action_episode_exp = []

  def create_agent(self):
    if self.sense_model is None:
      if path.exists(self.config.sense_model_filename):
        self.sense_model = torch.load(self.config.sense_model_filename, map_location=self.config.device)
      else:
        self.sense_model = RCSenseModel2(RCStateEncoder5.sense_dimension(), RCSenseEncoder3.dimension(), self.config.sense_hidden_size, 1)
    if self.action_model is None:
      if path.exists(self.config.action_model_filename):
        self.action_model = torch.load(self.config.action_model_filename, map_location=self.config.device)
      else:
        self.action_model = RCActionModel4(RCStateEncoder5.action_dimension(), RCActionEncoder4.dimension(), self.config.action_hidden_size, 1)
    action_exp = RCActionEpisode3()
    sense_exp = RCSenseEpisode2()
    self.action_episode_exp.append(action_exp)
    self.sense_episode_exp.append(sense_exp)
    agent = RCDaggerAgent2(
        RCStateEncoder5(),
        RCActionEncoder4(),
        RCSenseEncoder3(),
        self.action_model,
        self.sense_model,
        self.config.device,
        False, #TODO: make it flexible with episode
        action_exp,
        sense_exp,
        rc_sense_reward2
    )
    return agent

  def should_learn(self, episode):
    if episode == self.config.episodes - 1:
      return True
    num_episodes = max(self.sense_ec.size(), self.action_ec.size())
    if num_episodes >= self.config.eb_size:
      return True
    else:
      return False

  def learn(self, episode):
    self.learn_sense(episode)
    self.learn_action(episode)
    self.sense_ec = RCSenseEC2()
    self.action_ec = RCActionEC3()
    if self.config.snapshot_frequency > 0 and episode / self.config.snapshot_frequency > self.snapshot_count:
      action_model_snapshot_filename = "{}_{}.pt".format(self.config.action_model_snapshot_prefix, self.snapshot_count)
      torch.save(self.action_model, action_model_snapshot_filename)
      sense_model_snapshot_filename = "{}_{}.pt".format(self.config.sense_model_snapshot_prefix, self.snapshot_count)
      torch.save(self.sense_model, sense_model_snapshot_filename)
    if episode == self.config.episodes - 1:
      torch.save(self.action_model, self.config.action_model_filename)
      torch.save(self.sense_model, self.config.sense_model_filename)

  def learn_sense(self, episode):
    sense_eb = self.sense_ec.to_dataset()
    sense_loader = data.DataLoader(sense_eb, batch_size=self.config.batchsize, shuffle=True, pin_memory=True, num_workers=0)
    optimizer = self.sense_optimizer()
    loss = self.sense_loss()
    self.sense_model.train()
    for e in range(self.config.iterations):
      for i, (cs, a, r) in enumerate(sense_loader):
        optimizer.zero_grad()
        batchsize = a.shape[0]
        # cs in (b, S, F), a in (b, S, 1), r in (b, S, 1)
        cs, a, r = cs.to(self.config.device), a.to(self.config.device), r.to(self.config.device)
        h = self.sense_model.init(batchsize).to(self.config.device)
        pi, _ = self.sense_model(cs, h)
        pi = pi.reshape(pi.shape[0] * pi.shape[1], pi.shape[2])
        a = a.reshape(a.shape[0] * a.shape[1])
        r = r.reshape(r.shape[0] * r.shape[1])
        pi = torch.index_select(pi, 1, a).diagonal()
        l = loss(pi, r, self.config.pg_epsilon)
        l.backward()
        optimizer.step()
        self.reporter.train_sense_gather(episode, i, len(sense_eb), l.item())

  def sense_optimizer(self):
    return Adam(
        self.sense_model.parameters(),
        lr=self.config.learning_rate,
        weight_decay=self.config.weight_decay
    )

  def sense_loss(self):
    return PGError()

  def learn_action(self, episode):
    action_eb = self.action_ec.to_dataset()
    action_loader = data.DataLoader(action_eb, batch_size=self.config.batchsize, shuffle=True, pin_memory=True, num_workers=0)
    optimizer = self.action_optimizer()
    loss = self.action_loss()
    self.action_model.train()
    for e in range(self.config.iterations):
      for i, (cs, a) in enumerate(action_loader):
        optimizer.zero_grad()
        batchsize = a.shape[0]
        # cs in dim (b, S, F), a in (b, S, 1)
        cs, a = cs.to(self.config.device), a.to(self.config.device)
        h = self.action_model.init(batchsize).to(self.config.device)
        pi, _ = self.action_model(cs, h) # dim (b, S, A)
        pi = pi.reshape(pi.shape[0] * pi.shape[1], pi.shape[2])
        a = a.reshape(a.shape[0] * a.shape[1])
        l = loss(pi, a)
        l.backward()
        optimizer.step()
        self.reporter.train_action_gather(episode, i, len(action_eb), l.item())

  def action_optimizer(self):
    return Adam(
        self.action_model.parameters(),
        lr=self.config.learning_rate,
        weight_decay=self.config.weight_decay
    )

  def action_loss(self):
    return nn.CrossEntropyLoss()
