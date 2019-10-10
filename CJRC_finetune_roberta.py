import random
import os
import argparse
import numpy as np
import json
import torch
import utils
from models.pytorch_modeling import BertConfig, BertForQA_CLS
from optimizations.pytorch_optimization import get_optimization, warmup_linear
from evaluate.CJRC_output import write_predictions
from evaluate.cmrc2018_evaluate import get_eval_with_neg
import collections
from torch import nn
from torch.utils.data import TensorDataset, DataLoader
from tqdm import tqdm
import copy
from tokenizations import offical_tokenization as tokenization
from preprocess.CJRC_preprocess import json2features


def evaluate(model, args, eval_examples, eval_features, device, global_steps, best_f1, best_em):
    print("***** Eval *****")
    RawResult = collections.namedtuple("RawResult",
                                       ["unique_id", "start_logits", "end_logits", "target_logits"])
    output_prediction_file = os.path.join(args.checkpoint_dir,
                                          "predictions_steps" + str(global_steps) + ".json")
    output_nbest_file = output_prediction_file.replace('predictions', 'nbest')

    all_input_ids = torch.tensor([f['input_ids'] for f in eval_features], dtype=torch.long)
    all_input_mask = torch.tensor([f['input_mask'] for f in eval_features], dtype=torch.long)
    all_segment_ids = torch.tensor([f['segment_ids'] for f in eval_features], dtype=torch.long)
    all_example_index = torch.arange(all_input_ids.size(0), dtype=torch.long)

    eval_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_example_index)
    eval_dataloader = DataLoader(eval_data, batch_size=args.n_batch, shuffle=False)

    model.eval()
    all_results = []
    print("Start evaluating")
    for input_ids, input_mask, segment_ids, example_indices in tqdm(eval_dataloader, desc="Evaluating"):
        input_ids = input_ids.to(device)
        input_mask = input_mask.to(device)
        segment_ids = segment_ids.to(device)
        with torch.no_grad():
            (batch_start_logits, batch_end_logits, batch_target_logits), _ = model(input_ids, segment_ids, input_mask)

        for i, example_index in enumerate(example_indices):
            start_logits = batch_start_logits[i].detach().cpu().tolist()
            end_logits = batch_end_logits[i].detach().cpu().tolist()
            target_logits = batch_target_logits[i].detach().cpu().tolist()
            eval_feature = eval_features[example_index.item()]
            unique_id = int(eval_feature['unique_id'])
            all_results.append(RawResult(unique_id=unique_id,
                                         start_logits=start_logits,
                                         end_logits=end_logits,
                                         target_logits=target_logits))

    write_predictions(eval_examples, eval_features, all_results,
                      n_best_size=args.n_best, max_answer_length=args.max_ans_length,
                      do_lower_case=True, output_prediction_file=output_prediction_file,
                      output_nbest_file=output_nbest_file, version_2_with_negative=True,
                      null_score_diff_threshold=args.null_score_diff_threshold)

    tmp_result = get_eval_with_neg(args.dev_file, output_prediction_file)
    tmp_result['STEP'] = global_steps
    with open(args.log_file, 'a') as aw:
        aw.write(json.dumps(tmp_result) + '\n')
    print(tmp_result)

    if float(tmp_result['F1']) > best_f1:
        best_f1 = float(tmp_result['F1'])
        # utils.torch_save_model(model, args.checkpoint_dir, global_steps, max_save_num=1)
    if float(tmp_result['EM']) > best_em:
        best_em = float(tmp_result['EM'])

    model.train()

    return best_f1, best_em


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--gpu_ids', type=str, default='5,6')

    # training parameter
    parser.add_argument('--train_epochs', type=int, default=2)
    parser.add_argument('--n_batch', type=int, default=32)
    parser.add_argument('--lr', type=float, default=3e-5)
    parser.add_argument('--dropout', type=float, default=0.1)
    parser.add_argument('--clip_norm', type=float, default=1.0)
    parser.add_argument('--loss_scale', type=float, default=0)
    parser.add_argument('--warmup_iters', type=int, default=0.1)
    parser.add_argument("--schedule", default='warmup_linear', type=str, help='schedule')
    parser.add_argument("--weight_decay_rate", default=0.01, type=float, help='weight_decay_rate')
    parser.add_argument('--loss_count', type=int, default=1000)
    parser.add_argument('--seed', type=list, default=[123, 456, 789, 556, 977])
    parser.add_argument('--float16', type=int, default=True)  # only sm >= 7.0 (tensorcores)
    parser.add_argument('--max_ans_length', type=int, default=50)
    parser.add_argument('--n_best', type=int, default=20)
    parser.add_argument('--eval_epochs', type=float, default=0.5)
    parser.add_argument('--save_best', type=bool, default=True)
    parser.add_argument('--vocab_size', type=int, default=21128)
    parser.add_argument('--null_score_diff_threshold', type=float, default=0.0)

    # data dir
    parser.add_argument('--train_dir', type=str,
                        default='dataset/CJRC/train_features_roberta512.json')
    parser.add_argument('--dev_dir1', type=str,
                        default='dataset/CJRC/dev_examples_roberta512.json')
    parser.add_argument('--dev_dir2', type=str,
                        default='dataset/CJRC/dev_features_roberta512.json')
    parser.add_argument('--train_file', type=str,
                        default='origin_data/CJRC/train_data.json')
    parser.add_argument('--dev_file', type=str,
                        default='origin_data/CJRC/dev_data.json')
    parser.add_argument('--bert_config_file', type=str,
                        default='check_points/pretrain_models/roberta_wwm_ext_base/bert_config.json')
    parser.add_argument('--vocab_file', type=str,
                        default='check_points/pretrain_models/roberta_wwm_ext_base/vocab.txt')
    parser.add_argument('--init_restore_dir', type=str,
                        default='check_points/pretrain_models/roberta_wwm_ext_base/pytorch_bert_model.pth')
    parser.add_argument('--checkpoint_dir', type=str,
                        default='check_points/CJRC/roberta_wwm_ext_base/')
    parser.add_argument('--setting_file', type=str, default='setting.txt')
    parser.add_argument('--log_file', type=str, default='log.txt')

    # use some global vars for convenience
    args = parser.parse_args()
    args.checkpoint_dir += ('/epoch{}_batch{}_lr{}_warmup{}_anslen{}/'
                            .format(args.train_epochs, args.n_batch, args.lr, args.warmup_iters, args.max_ans_length))
    args = utils.check_args(args)
    os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu_ids
    device = torch.device("cuda")
    n_gpu = torch.cuda.device_count()
    print("device %s n_gpu %d" % (device, n_gpu))
    print("device: {} n_gpu: {} 16-bits training: {}".format(device, n_gpu, args.float16))

    bert_config = BertConfig.from_json_file(args.bert_config_file)

    # load data
    print('loading data...')
    tokenizer = tokenization.BertTokenizer(vocab_file=args.vocab_file, do_lower_case=True)
    assert args.vocab_size == len(tokenizer.vocab)
    if not os.path.exists(args.train_dir):
        json2features(args.train_file, [args.train_dir.replace('_features_', '_examples_'), args.train_dir],
                      tokenizer, is_training=True,
                      max_seq_length=bert_config.max_position_embeddings)

    if not os.path.exists(args.dev_dir1) or not os.path.exists(args.dev_dir2):
        json2features(args.dev_file, [args.dev_dir1, args.dev_dir2], tokenizer, is_training=False,
                      max_seq_length=bert_config.max_position_embeddings)

    train_features = json.load(open(args.train_dir, 'r'))
    dev_examples = json.load(open(args.dev_dir1, 'r'))
    dev_features = json.load(open(args.dev_dir2, 'r'))
    if os.path.exists(args.log_file):
        os.remove(args.log_file)

    steps_per_epoch = len(train_features) // args.n_batch
    eval_steps = int(steps_per_epoch * args.eval_epochs)
    dev_steps_per_epoch = len(dev_features) // args.n_batch
    if len(train_features) % args.n_batch != 0:
        steps_per_epoch += 1
    if len(dev_features) % args.n_batch != 0:
        dev_steps_per_epoch += 1
    total_steps = steps_per_epoch * args.train_epochs
    warmup_rate = copy.deepcopy(args.warmup_iters)
    args.warmup_iters = int(args.warmup_iters * total_steps)

    print('steps per epoch:', steps_per_epoch)
    print('total steps:', total_steps)
    print('warmup steps:', args.warmup_iters)

    F1s = []
    EMs = []

    for seed_ in args.seed:
        best_f1, best_em = 0, 0
        with open(args.log_file, 'a') as aw:
            aw.write('===================================' +
                     'SEED:' + str(seed_)
                     + '===================================' + '\n')
        print('SEED:', seed_)

        random.seed(seed_)
        np.random.seed(seed_)
        torch.manual_seed(seed_)
        if n_gpu > 0:
            torch.cuda.manual_seed_all(seed_)

        # init model
        print('init model...')
        model = BertForQA_CLS(bert_config)
        utils.torch_show_all_params(model)
        utils.torch_init_model(model, args.init_restore_dir)
        if args.float16:
            model.half()
        model.to(device)
        if n_gpu > 1:
            model = torch.nn.DataParallel(model)

        optimizer = get_optimization(model=model,
                                     float16=args.float16,
                                     learning_rate=args.lr,
                                     loss_scale=args.loss_scale,
                                     total_steps=total_steps,
                                     schedule=args.schedule,
                                     warmup_iters=args.warmup_iters,
                                     weight_decay_rate=args.weight_decay_rate)

        all_input_ids = torch.tensor([f['input_ids'] for f in train_features], dtype=torch.long)
        all_input_mask = torch.tensor([f['input_mask'] for f in train_features], dtype=torch.long)
        all_segment_ids = torch.tensor([f['segment_ids'] for f in train_features], dtype=torch.long)

        seq_len = all_input_ids.shape[1]
        # 样本长度不能超过bert的长度限制
        assert seq_len <= bert_config.max_position_embeddings

        # true label
        all_start_positions = torch.tensor([f['start_position'] for f in train_features], dtype=torch.long)
        all_end_positions = torch.tensor([f['end_position'] for f in train_features], dtype=torch.long)
        all_target_labels = torch.tensor([f['target_label'] for f in train_features], dtype=torch.long)

        train_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids,
                                   all_start_positions, all_end_positions, all_target_labels)
        train_dataloader = DataLoader(train_data, batch_size=args.n_batch, shuffle=True)

        print('***** Training *****')
        model.train()
        global_steps = 1
        best_em = 0
        best_f1 = 0
        for i in range(int(args.train_epochs)):
            print('Starting epoch %d' % (i + 1))
            total_loss = 0
            iteration = 1
            with tqdm(total=steps_per_epoch, desc='Epoch %d' % (i + 1)) as pbar:
                for step, batch in enumerate(train_dataloader):
                    batch = tuple(t.to(device) for t in batch)
                    input_ids, input_mask, segment_ids, start_positions, end_positions, all_target_labels = batch
                    loss = model(input_ids, segment_ids, input_mask, start_positions, end_positions, all_target_labels)
                    if type(loss) == tuple:
                        loss = loss[0]
                    if n_gpu > 1:
                        loss = loss.mean()  # mean() to average on multi-gpu.
                    total_loss += loss.item()
                    pbar.set_postfix({'loss': '{0:1.5f}'.format(total_loss / (iteration + 1e-5))})
                    pbar.update(1)

                    if args.float16:
                        optimizer.backward(loss)
                    else:
                        loss.backward()

                    if args.float16:
                        # modify learning rate with special warm up BERT uses
                        # if args.fp16 is False, BertAdam is used and handles this automatically
                        lr_this_step = args.lr * warmup_linear(global_steps / total_steps, warmup_rate)
                        for param_group in optimizer.param_groups:
                            param_group['lr'] = lr_this_step
                        optimizer.step()  # We have accumulated enought gradients
                        model.zero_grad()
                        global_steps += 1
                        iteration += 1

                    if global_steps % eval_steps == 0:
                        best_f1, best_em = evaluate(model, args, dev_examples, dev_features, device, global_steps,
                                                    best_f1, best_em)

        F1s.append(best_f1)
        EMs.append(best_em)

        # release the memory
        del model
        del optimizer
        torch.cuda.empty_cache()

    print('Mean F1:', np.mean(F1s), 'Mean EM:', np.mean(EMs))
    print('Best F1:', np.max(F1s), 'Best EM:', np.max(EMs))
    with open(args.log_file, 'a') as aw:
        aw.write('Mean(Best) F1:{}({})\n'.format(np.mean(F1s), np.max(F1s)))
        aw.write('Mean(Best) EM:{}({})\n'.format(np.mean(EMs), np.max(EMs)))
