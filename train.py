import os
import wandb
import torch
import random
import numpy as np
import importlib
import multiprocessing
from dotenv import load_dotenv
from datasets import load_dataset
from utils.metric import compute_metrics
from utils.encoder import Encoder, BartEncoder, T5Encoder
from utils.collator import DataCollatorWithPadding
from utils.preprocessor import AnnotationPreprocessor, FunctionPreprocessor, BasePreprocessor
from trainer import ImprovedRDropTrainer
from arguments import ModelArguments, DataTrainingArguments, MyTrainingArguments, LoggingArguments

from transformers import (
    Trainer,
    AutoConfig,
    AutoTokenizer,
    AutoModelForSequenceClassification,
    HfArgumentParser,
)


def main():
    parser = HfArgumentParser(
        (ModelArguments, DataTrainingArguments, MyTrainingArguments, LoggingArguments)
    )
    model_args, data_args, training_args, logging_args = parser.parse_args_into_dataclasses()
    output_dir = training_args.output_dir
    seed_everything(training_args.seed)

    load_dotenv(dotenv_path=logging_args.dotenv_path)
    POOLC_AUTH_KEY = os.getenv("POOLC_AUTH_KEY")

    # -- Loading datasets
    for fold_index in range(0, 5):
        if data_args.do_all:
            dataset_name = f"PoolC/all-clone-detection_v2"
        else: # kfold is implmented
            dataset_name = f"PoolC/{fold_index+1}-fold-clone-detection-600k-5fold"

        dset = load_dataset(dataset_name, use_auth_token=True)

        if data_args.do_all:
            random_numbers_train = np.random.randint(
                0, len(dset["train"]), int(179000 * 6)
            )  # can be set as a parameter if K-Fold is used
            dset["train"] = dset["train"].select(random_numbers_train)
            dset["val"] = dset["val"]
        else: # kfold is used
            random_numbers_train = np.random.randint(
                0, len(dset["train"]), int(179000 * 5)
            )  # can be set as a parameter if K-Fold is used
            dset["train"] = dset["train"].select(random_numbers_train)
            random_numbers_val = np.random.randint(
                0, len(dset["val"]), int(179000)
            )  # can be set as a parameter if K-Fold is used
            dset["val"] = dset["val"].select(random_numbers_val)
        print(dset)

        CPU_COUNT = multiprocessing.cpu_count() // 2
        MODEL_NAME = training_args.model_name
        PLM_NAME = model_args.PLM
        MAX_LENGTH = 4000

        def filter_fn(data):
            if len(data["code1"]) >= MAX_LENGTH or len(data["code2"]) >= MAX_LENGTH:
                return False
            else:
                return True

        dset = dset.filter(filter_fn, num_proc=CPU_COUNT)
        print(dset)

        # -- Preprocessing datasets
        if "bert" in PLM_NAME.lower():
            fn_preprocessor = FunctionPreprocessor()
            dset = dset.map(fn_preprocessor, batched=True, num_proc=CPU_COUNT)
            an_preprocessor = AnnotationPreprocessor()
            dset = dset.map(an_preprocessor, batched=True, num_proc=CPU_COUNT)
        elif "t5" in PLM_NAME.lower() or "bart" in PLM_NAME.lower():
            preprocessor = BasePreprocessor()
            dset = dset.map(preprocessor, batched=True, num_proc=CPU_COUNT)
        # -- Tokenizing & Encoding
        MODEL_CATEGORY = training_args.model_category
        tokenizer = AutoTokenizer.from_pretrained(model_args.PLM)

        if "bert" in PLM_NAME.lower():
            dataset_encoder_class = Encoder
            print("Using BertEncoder")
        elif "t5" in PLM_NAME.lower():
            dataset_encoder_class = T5Encoder
            print("T5Encoder")
        elif "bart" in MODEL_NAME.lower():
            dataset_encoder_class = BartEncoder
            print("BartEncoder")

        encoder = dataset_encoder_class(
            tokenizer, model_category=MODEL_CATEGORY, max_input_length=data_args.max_length
        )
        dset = dset.map(
            encoder, batched=True, num_proc=CPU_COUNT, remove_columns=dset["train"].column_names
        )
        print(dset)

        # -- Config & Model Class
        config = AutoConfig.from_pretrained(model_args.PLM)
        config.num_labels = 2
        config.tokenizer_cls_token_id = tokenizer.cls_token_id
        config.tokenizer_sep_token_id = tokenizer.sep_token_id
        config.dropout_rate = 0.1

        if MODEL_NAME == "base":
            model_class = AutoModelForSequenceClassification
        else:
            model_category = importlib.import_module("models." + MODEL_CATEGORY)
            model_class = getattr(model_category, MODEL_NAME)

        # -- Collator
        data_collator = DataCollatorWithPadding(
            tokenizer=tokenizer, max_length=data_args.max_length
        )

        if training_args.do_train:
            training_args.remove_unused_columns = False
            model = model_class.from_pretrained(model_args.PLM, config=config)

            # -- Wandb
            WANDB_AUTH_KEY = os.getenv("WANDB_AUTH_KEY")
            wandb.login(key=WANDB_AUTH_KEY)

            if training_args.max_steps == -1:
                name = f"EP:{training_args.num_train_epochs}_"
            else:
                name = f"MS:{training_args.max_steps}_"

            name += f"LR:{training_args.learning_rate}_BS:{training_args.per_device_train_batch_size}_WR:{training_args.warmup_ratio}_WD:{training_args.weight_decay}_"
            name += MODEL_NAME
            if data_args.do_all:
                name += "_all"
            else:
                name += f"_{fold_index+1}_fold"
            num_train = str(len(random_numbers_train))[:-3] + "k"
            name += f"_{num_train}"

            if data_args.do_all:
                training_args.output_dir = os.path.join(output_dir, f"all_{MODEL_NAME}")
            else:
                training_args.output_dir = os.path.join(
                    output_dir, f"{fold_index+1}_fold_{MODEL_NAME}"
                )

            wandb.init(
                entity="poolc", project=logging_args.project_name, group=model_args.PLM, name=name
            )
            wandb.config.update(training_args)
            if "bert" in MODEL_NAME.lower():
                print("using RDrop applied trainer")
                trainer = ImprovedRDropTrainer(  # the instantiated 🤗 Transformers model to be trained
                    model=model,  # model
                    args=training_args,  # training arguments, defined above
                    train_dataset=dset["train"],  # training dataset
                    eval_dataset=dset["val"],  # evaluation dataset
                    data_collator=data_collator,  # collator
                    tokenizer=tokenizer,  # tokenizer
                    compute_metrics=compute_metrics,  # define metrics function
                )
            elif "t5" in MODEL_NAME.lower() or "bart" in MODEL_NAME.lower():
                print("using huggingface trainer")
                trainer = Trainer(  # the instantiated 🤗 Transformers model to be trained
                    model=model,  # model
                    args=training_args,  # training arguments, defined above
                    train_dataset=dset["train"],  # training dataset
                    eval_dataset=dset["val"],  # evaluation dataset
                    data_collator=data_collator,  # collator
                    tokenizer=tokenizer,  # tokenizer
                    compute_metrics=compute_metrics,  # define metrics function
                )

            # -- Training
            trainer.train()
            trainer.evaluate()
            trainer.save_model(model_args.save_path)
            wandb.finish()
        if data_args.do_all:
            break
        else:
            pass


def seed_everything(seed):
    os.environ["PYTHONHASHSEED"] = str(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    np.random.seed(seed)
    np.random.default_rng(seed)
    random.seed(seed)


if __name__ == "__main__":
    main()
