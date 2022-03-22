## Tokenization repair using Transformers

### Installation

This project is mainly tested with Python 3.8, but should work fine with Python 3.6 and newer versions

```bash
git clone git@github.com:bastiscode/trt.git
cd trt

# if you just want to use pretrained models
pip install .
# alternatively, if you also want to train your own models
pip install .[train]

```

### Usage

#### From python

```python
from trt import TokenizationRepairer, get_available_models

# list all available models
print(get_available_models())

# create a tokenization repair instance, using the default pretrained model
tok_rep = TokenizationRepairer.from_pretrained()

# repair single strings
repaired_string = tok_rep.repair_text("p l e se,repiar thissen ten se!")

# repair multiple strings at once
repaired_strings = tok_rep.repair_text([
    "p l e se,repiar thissen ten se!",
    "alsosplitthissentenceforme"
])

# repair text file (every line is treated as a separate sequence to repair)
repaired_lines = tok_rep.repair_file("path/to/file.txt")
# optionally specify a output file
repaired_lines = tok_rep.repair_file("path/to/file.txt", output_file_path="save/output/here.txt")
```

#### From command line

After installation the command `trt` is available in your python environment. It lets you use the tokenization repair
models directly from the command line. Below are examples of how to use `trt`. See `trt -h` for all options.

```bash
# print version
trt -v

# list available models
trt -l

# by default trt tries to read stdin, repairs the input it got line by line 
# and prints the repaired lines back out
# therefore, you can for example use trt with pipes
echo "splitthissentenceforme" | trt
cat "path/to/input/file.txt" | trt > output.txt

# repair a string using
trt -r "splitthissentenceforme"

# repair a text file line by line and print the repaired lines
trt -f path/to/input/file.txt
# optionally specify an output file path where the repaired lines are saved
trt -f path/to/input/file.txt -o output.txt

# start an interactive tokenization repair session
# where your input will be repaired and printed back out
trt -i

# start a tokenization repair server on <host>:<port> with the following endpoints:
### <host>:<port>/models [GET] --> list available models as json 
### <host>:<port>/repair_text?text=texttorepair [GET] --> repaired text as json
### <host>:<port>/repair_file [POST] --> repaired file as json
### To specify which model to use, you can use the model query parameter 
### (e.g. <host>:<port>/repair_file?model=eo_small_arxiv_with_errors), default model is eo_arxiv_with_errors
trt --server localhost:12345

### OPTIONS
### Pass the following flags to the trt command to customize its behaviour
-m <model_name> # use a different tokenization repair model than the default one 
--cpu # force execution on CPU, by default a GPU is used if available
--progress # display a progress bar (always on when a file is repaired using -f)
-b <batch_size> # specify a different batch size
-u # do not sort the inputs before repairing
-p # switch on pipe mode (useful but not needed when using Linux pipes)
-e <experiment_dir> # specify the path to an experiment directory to load the model from 
                    # (equivalent to TokenizationRepairer.from_experiment(experiment_dir) in python API)
--force-download # force download of the tokenization repair model even if it was already downloaded
--report <file_path> # save a report on the runtime of the model in form of a markdown table in a file
```

> Note: When first using `trt` with a pretrained model, the model needs to be downloaded, so depending on
> your internet speed the command might take considerably longer.

> Note: Loading the tokenization repair model requires an initial startup time each time you
> invoke the `trt` command. CPU startup time is around 1s, GPU startup time around 3.5s, so for small
> inputs or files you should probably pass the `-c` flag to force CPU execution for best performance.

### Documentation

#### Use pretrained model

If you just want to use this project to repair tokenization, this is the recommended way. See the available model for
the names of all available

```python
from trt import TokenizationRepairer, get_available_models

tok_rep = TokenizationRepairer.from_pretrained(
    # pretrained model to load, get all available models from get_available_models() 
    # (eo_arxiv_with_errors by default)
    model="eo_arxiv_with_errors",
    # whether to use a GPU if available or not 
    # (True by default)
    use_gpu=True,
    # optional path to a cache directory where pretrained models will be downloaded to,
    # if None, we check the env variable TOKENIZATION_REPAIR_CACHE_DIR, if it is not set 
    # we use a default cache directory at <trt_install_path>/api/.cache 
    # (None by default)
    cache_dir=None,
)
```

#### Use own model

Once you trained you model you can use it in the following way

```python
from trt import TokenizationRepairer

tok_rep = TokenizationRepairer.from_experiment(
    # path to the experiment directory that is created by your training run
    experiment_dir="path/to/experiment_dir",
    # whether to use a GPU if available or not 
    # (True by default)
    use_gpu=True
)
```

See [tokenization_repair/README.md](tokenization_repair/README.md) for instructions to train you own model.

### Directory structure

The most important directories you might want to look at are:

```
configs -> (example yaml config files for data preprocessing, models and training)
trt -> (library code used by this project)
tests -> (unit tests for trt library)
tokenization_repair -> (actual tokenization repair project directory)
    benchmark   -> (benchmarks, benchmark results and functionality to run and evaluate benchmarks)
    configs     -> (yaml config files used to preprocess data and 
                    train the tokenization repair models)
```

### Docker

You can also run this project using docker. Build the image using

`docker build -t tokenization_repair_transformers .`

After that you can run the project using

```
docker run -it [--gpus all] tokenization_repair_transformers

Note
-----
Make sure you have docker version >= 19.03, a nvidia driver
and the nvidia container toolkit installed (see https://github.com/NVIDIA/nvidia-docker)
if you want to run the container with GPU support. You also need to add '--gpus all' 
to the 'docker run' command from above.
```

Inside the container the [`trt` command](#from-command-line) is available to you.

If you e.g. have some text files you want to repair using the pretrained models start your container using
`docker run --gpus all -v <directory_path>:/text_files -it tokenization_repair_transformers` where `<directory_path>`
contains the text files. Then inside the container you can repair them
with `trt -f /text_files/file_1.txt -o /text_files/file_1_repaired.txt`.

You can also start a tokenization repair server inside the container using `trt --server 0.0.0.0:<port>`. Keep in mind
that if you want to access the server from outside the container you have to expose the port using
`docker run --gpus all -p <outside_port>:<port> -it tokenization_repair_transformers`.
