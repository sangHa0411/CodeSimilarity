import os
import importlib
import numpy as np
import pandas as pd
import multiprocessing
from datasets import Dataset
from trainer import Trainer, ImprovedRDropTrainer
from utils.encoder import Encoder, BartEncoder, T5Encoder
from utils.collator import DataCollatorWithPadding
from utils.preprocessor import AnnotationPreprocessor, FunctionPreprocessor, BasePreprocessor
from trainer import ImprovedRDropTrainer
from arguments import ModelArguments, DataTrainingArguments, MyTrainingArguments, InferenceArguments

from transformers import (
    Trainer,
    AutoConfig,
    AutoTokenizer,
    AutoModelForSequenceClassification,
    HfArgumentParser,
)


def main():
    parser = HfArgumentParser(
        (ModelArguments, DataTrainingArguments, MyTrainingArguments, InferenceArguments)
    )
    model_args, data_args, training_args, inference_args = parser.parse_args_into_dataclasses()

    # -- Loading datasets
    # transformers.logging.set_verbosity_error()
    df = pd.read_csv(os.path.join(data_args.date_path, "test.csv"))
    dset = Dataset.from_pandas(df)
    print(dset)

    CPU_COUNT = multiprocessing.cpu_count() // 2
    MODEL_NAME = training_args.model_name
    PLM_NAME = model_args.PLM

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
        encoder,
        batched=True,
        num_proc=multiprocessing.cpu_count(),
        remove_columns=dset.column_names,
    )
    print(dset)

    # -- Model Class
    MODEL_NAME = training_args.model_name

    if MODEL_NAME == "base":
        model_class = AutoModelForSequenceClassification
    else:
        model_category = importlib.import_module("models." + MODEL_CATEGORY)
        model_class = getattr(model_category, MODEL_NAME)

    # -- Collator
    data_collator = DataCollatorWithPadding(tokenizer=tokenizer, max_length=data_args.max_length)

    # -- Inference
    # -- Config & Model
    config = AutoConfig.from_pretrained(model_args.PLM)
    config.dropout_rate = 0.1
    model = model_class.from_pretrained(model_args.PLM, config=config)
    training_args.remove_unused_columns = False
    if "bert" in MODEL_NAME.lower():
        print("using RDrop applied trainer")
        trainer_class = ImprovedRDropTrainer
    elif "t5" in MODEL_NAME.lower() or "bart" in MODEL_NAME.lower():
        print("using huggingface trainer")
        trainer_class = Trainer
    trainer = trainer_class(  # the instantiated 🤗 Transformers model to be trained
        model=model,  # trained model
        args=training_args,  # training arguments, defined above
        data_collator=data_collator,  # collator
    )

    # -- Inference
    outputs = trainer.predict(dset)

    pred_ids = np.argmax(outputs[0], axis=-1)
    sub_df = pd.read_csv(os.path.join(data_args.date_path, "sample_submission.csv"))
    sub_df["similar"] = pred_ids

    sub_df.to_csv(os.path.join(training_args.output_dir, inference_args.file_name), index=False)


if __name__ == "__main__":
    main()
