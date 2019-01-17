import dill
from .configGenerator import sample_config, flatten_dict
import subprocess
import collections
import pandas as pd
import torch
import concurrent
import distutils
from functools import partial
from oil.tuning.slurmExecutor import SlurmExecutor, LocalExecutor


def slurm_available():
    return distutils.spawn.find_executable('salloc') is not None

DEFAULT_SLURM_SETTINGS = {
    'N':1,
    'c':1, # Number of cores per node
    'mem':24000, # mem specifies the maximum virtual memory which includes max GPU ram
    'time': '24:00:00',
    'partition':'default_gpu',
    'gres':'gpu:1080ti:1',
}

class Study(object):
    """ The study object allows hyperparameter search experimentation, with
        automatic trial parrallelization via Slurm if available. The trial configs
        that are run are stored in the dataframe self.configs, 
        and trial outcomes in self.outcomes. """
    def __init__(self, perform_trial, config_spec,
                    slurm_cfg={}, study_name=None):
        self.perform_trial = perform_trial
        self.config_spec = config_spec
        slurm_settings = {**DEFAULT_SLURM_SETTINGS,**slurm_cfg}
        self.Executor = partial(SlurmExecutor,slurm_cfg=slurm_settings) \
                               if slurm_available() else LocalExecutor
        self.configs = pd.DataFrame()
        self.outcomes = pd.DataFrame()
        self.name = study_name or __file__[:-3]

    def flat_configs(self):
        """ Return a dataframe where rows are flattened versions of self.configs"""
        flat_cfgs = pd.DataFrame()
        for row in self.configs.apply(flatten_dict,axis=1):
            flat_cfgs.append(row,ignore_index=True)
        return flat_cfgs

    def run(self, num_trials, max_workers=10, new_config_spec=None):
        """ runs the study with num_trials and max_workers slurm nodes
            trials are executed in parallel by the slurm nodes, study object
            is updated and saved as results come in """
        if new_config_spec: self.config_spec=new_config_spec
        with self.Executor(max_workers) as executor:
            futures = [executor.submit(self.perform_trial,
                        sample_config(self.config_spec),i) for i in range(num_trials)]
            for future in concurrent.futures.as_completed(futures):
                cfg, outcome = future.result()
                self.configs.append(cfg,ignore_index=True)
                self.outcomes.append(outcome,ignore_index=True)
                torch.save(self,self.name+'.s',pickle_module=dill)

def train_trial(make_trainer):
    """ a common trainer trial use case: make_trainer, train, return cfg and emas"""
    def _perform_trial(cfg,i):
        cfg['trainer_config']['log_dir'] += 'trial{}/'.format(i)
        trainer = make_trainer(cfg)
        trainer.logger.add_scalars('Config',flatten_dict(cfg))
        outcome = trainer.train(cfg['num_epochs'])
        save_path = trainer.default_save_path(suffix='.trainer')
        torch.save(trainer,save_path,pickle_module=dill)
        outcome['saved_at']=save_path
        return cfg, outcome
    return _perform_trial