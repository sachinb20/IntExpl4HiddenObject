## Project introduction

Scene representation is one of the key design
choices that can facilitate downstream planning for a variety of
tasks. Scene Graphs are a scalable and efficient representation
as the volume of the scene and the duration of the robot’s
operation increases. While most of the current literature
focuses only on incorporating visible objects in a scene-graph,
in this project, we focus on the hidden and occluded objects by
interacting in the environment through a learned policy and
consequently building an Action-Conditioned Scene Graph.
The repo with the code for our work can be found here.


## Requirements
Install required packages:
```
conda install pytorch==1.6.0 torchvision==0.7.0 cudatoolkit=10.1 -c pytorch
pip install -r requirements.txt
```

Install AI2-Thor
```
pip install ai2thor
```

## Train the Hybrid variant

Before running the training or evaluation scripts, set the environment variables according to the variant you want to use.

####  Setting Up Environment Variables 

```bash
export E2E=false
export OBCOV=false
export HYBRID=true
```

### Training 

To train a agent on GPU 0, Display :0
```
python -m interaction_exploration.run \
    --config interaction_exploration/config/intexpGT.yaml \
    --mode train \
    SEED 0 TORCH_GPU_ID 0 X_DISPLAY :0 \
    ENV.NUM_STEPS 256 \
    NUM_PROCESSES 16 \
    CHECKPOINT_FOLDER interaction_exploration/cv/Hybrid/run0/ \

```


## Policy Evaluation

### Hybrid

```bash
export E2E=false
export OBCOV=false
export HYBRID=true
```

```
python -m interaction_exploration.run   \
    --config interaction_exploration/config/intexpGT.yaml  \     
    --mode eval \
    ENV.NUM_STEPS 1024     NUM_PROCESSES 1    \
    EVAL.DATASET interaction_exploration/data/test_episodes_K_16.json \    
    TORCH_GPU_ID 0 X_DISPLAY :1   \
    CHECKPOINT_FOLDER interaction_exploration/checkpoints/intexpGT_Traverse_open_close/run0/ \
    LOAD   interaction_exploration/checkpoints/intexpGT_Traverse_open_close/run0/ckpt.6.pth \
```

### E2E

```bash
export E2E=true
export OBCOV=false
export HYBRID=false
```

```
python -m interaction_exploration.run     
    --config interaction_exploration/config/intexpGT.yaml     
    --mode eval     ENV.NUM_STEPS 1024     NUM_PROCESSES 1     
    EVAL.DATASET interaction_exploration/data/test_episodes_K_16.json     
    TORCH_GPU_ID 0 X_DISPLAY :1     
    CHECKPOINT_FOLDER interaction_exploration/checkpoints/intexpGT_Traverse_E2E_new/run0/ 
    LOAD   interaction_exploration/checkpoints/intexpGT_Traverse_E2E_new/run0/ckpt.8.pth 
```

### ObjCov

```bash
export E2E=false
export OBCOV=true
export HYBRID=false
```

```
python -m interaction_exploration.run     
    --config interaction_exploration/config/intexpGT_no_interact.yaml     
    --mode eval     ENV.NUM_STEPS 1024     NUM_PROCESSES 1     
    EVAL.DATASET interaction_exploration/data/test_episodes_K_16.json     
    TORCH_GPU_ID 0 X_DISPLAY :1     
    CHECKPOINT_FOLDER interaction_exploration/checkpoints/intexpGT_Traverse_no_interact/run0/ 
    LOAD   interaction_exploration/checkpoints/intexpGT_Traverse_no_interact/run0/ckpt.4.pth 
```


