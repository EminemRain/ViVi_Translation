# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
# Copyright 2017, Center of Speech and Language of Tsinghua University.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""
Binary for training translation models and decoding from them.
Running this program without --decode will start training a model saving checkpoints to --train_dir.
Running with --decode will start decoding the testing set using a trained model specified by --model.

See the following paper for more information on memory-augmented neural machine translation model.
 * https://arxiv.org/abs/1708.02005
 * https://arxiv.org/abs/1706.08683
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math
import os
import sys
import time
import numpy as np
from six.moves import xrange
import tensorflow as tf
import pickle as pkl

sys.path.append(".")
import data_utils
import seq2seq_model

tf.app.flags.DEFINE_float("learning_rate", 0.0005, "Learning rate.")
tf.app.flags.DEFINE_float("learning_rate_decay_factor", 0.99,
                          "Learning rate decays by this much.")
tf.app.flags.DEFINE_float("max_gradient_norm", 1.0,
                          "Clip gradients to this norm.")
tf.app.flags.DEFINE_integer("batch_size", 80,
                            "Batch size to use during training.")
tf.app.flags.DEFINE_integer("hidden_units", 1000, "Size of hidden units for each layer.")
tf.app.flags.DEFINE_integer("hidden_edim", 500, "the dimension of word embedding.")
tf.app.flags.DEFINE_integer("num_layers", 1, "Number of layers in the model.")
tf.app.flags.DEFINE_integer("keep_prob", 0.8, "The keep probability used for dropout.")
tf.app.flags.DEFINE_integer("src_vocab_size", 30000, "Source vocabulary size.")
tf.app.flags.DEFINE_integer("trg_vocab_size", 30000, "Target vocabulary size.")
tf.app.flags.DEFINE_string("data_dir", "./data", "Data directory")
tf.app.flags.DEFINE_string("train_dir", "./MNMT/train", "Training directory.")
tf.app.flags.DEFINE_integer("steps_per_checkpoint", 1000,
                            "How many training steps to do per checkpoint.")
tf.app.flags.DEFINE_boolean("decode", False,
                            "Set to True for interactive decoding.")
tf.app.flags.DEFINE_string("model", "translate.ckpt-nmt", "The trained NMT model to load.")
tf.app.flags.DEFINE_string("model2", "", "the checkpoint mem model to load")
tf.app.flags.DEFINE_integer("beam_size", 5,
                            "The size of beam search. Do greedy search when set this to 1.")

FLAGS = tf.app.flags.FLAGS

tf.set_random_seed(123)

# We use a number of buckets and pad to the closest one for efficiency.
# See seq2seq_model.Seq2SeqModel for details of how they work.
if FLAGS.decode:
    # add one more bucket for longer sentences in testing set
    _buckets = [(10, 10), (20, 20), (30, 30), (40, 40), (50, 50), (100, 100)]
else:
    _buckets = [(10, 10), (20, 20), (30, 30), (40, 40), (50, 50)]



def read_data(source_path, target_path):
    """Read data from source and target files and put into buckets.

    Args:
      source_path: path to the files with token-ids for the source language.
      target_path: path to the file with token-ids for the target language;
        it must be aligned with the source file: n-th line contains the desired
        output for n-th line from the source_path.

    Returns:
      data_set: a list of length len(_buckets); data_set[n] contains a list of
        (source, target) pairs read from the provided data files that fit
        into the n-th bucket, i.e., such that len(source) < _buckets[n][0] and
        len(target) < _buckets[n][1]; source and target are lists of token-ids.
    """
    data_set = [[] for _ in _buckets]
    with tf.gfile.GFile(source_path, mode="r") as source_file:
        with tf.gfile.GFile(target_path, mode="r") as target_file:
            source, target = source_file.readline(), target_file.readline()
            counter = 0
            while source and target:
                counter += 1
                if counter % 100000 == 0:
                    print("  reading data line %d" % counter)
                    sys.stdout.flush()
                source_ids = [int(x) for x in source.split()]
                target_ids = [int(x) for x in target.split()]
                source_ids.append(data_utils.EOS_ID)
                target_ids.append(data_utils.EOS_ID)
                for bucket_id, (source_size, target_size) in enumerate(_buckets):
                    if len(source_ids) < source_size and len(target_ids) < target_size:
                        data_set[bucket_id].append([source_ids, target_ids])
                        break
                source, target = source_file.readline(), target_file.readline()
    return data_set


def create_model(session, forward_only, ckpt_file=None, ckpt_file2=None):
    """Create translation model and initialize or load parameters in session."""
    model = seq2seq_model.Seq2SeqModel(
            FLAGS.src_vocab_size, FLAGS.trg_vocab_size, _buckets,
            FLAGS.hidden_edim, FLAGS.hidden_units, FLAGS.num_layers,
            FLAGS.keep_prob, FLAGS.max_gradient_norm, FLAGS.batch_size,
            FLAGS.learning_rate, FLAGS.learning_rate_decay_factor,
            FLAGS.beam_size,
            forward_only=forward_only)
    if ckpt_file and not ckpt_file2:
        model_path = os.path.join(FLAGS.train_dir, ckpt_file)
        if tf.gfile.Exists(model_path):
            sys.stderr.write("Reading model parameters from %s\n" % model_path)
            sys.stderr.flush()
            model.saver_old.restore(session, model_path)
            params = tf.all_variables()
            # only initialize memory attention parameters.
            params = [p for p in params if p.name in
                      [
                          u'Variable:0', u'Variable_1:0',
                          u'beta1_power:0', u'beta2_power:0',
                          u'embedding_attention_seq2seq/embedding_attention_decoder/attention_decoder/attention/AttnVt_0:0',
                          u'embedding_attention_seq2seq/embedding_attention_decoder/attention_decoder/attention/AttnWt_0:0',
                          u'embedding_attention_seq2seq/embedding_attention_decoder/attention_decoder/attention/AttnU_0/Linear_mem/Matrix:0',
                          u'embedding_attention_seq2seq/embedding_attention_decoder/attention_decoder/attention/AttnU_0/Linear_mem/Bias:0',
                          u'embedding_attention_seq2seq/embedding_attention_decoder/attention_decoder/attention/AttnVt_0/Adam:0',
                          u'embedding_attention_seq2seq/embedding_attention_decoder/attention_decoder/attention/AttnVt_0/Adam_1:0',
                          u'embedding_attention_seq2seq/embedding_attention_decoder/attention_decoder/attention/AttnWt_0/Adam:0',
                          u'embedding_attention_seq2seq/embedding_attention_decoder/attention_decoder/attention/AttnWt_0/Adam_1:0',
                          u'embedding_attention_seq2seq/embedding_attention_decoder/attention_decoder/attention/AttnU_0/Linear_mem/Matrix/Adam:0',
                          u'embedding_attention_seq2seq/embedding_attention_decoder/attention_decoder/attention/AttnU_0/Linear_mem/Matrix/Adam_1:0',
                          u'embedding_attention_seq2seq/embedding_attention_decoder/attention_decoder/attention/AttnU_0/Linear_mem/Bias/Adam:0',
                          u'embedding_attention_seq2seq/embedding_attention_decoder/attention_decoder/attention/AttnU_0/Linear_mem/Bias/Adam_1:0',

                      ]]
            session.run(tf.initialize_variables(params))
    elif ckpt_file and ckpt_file2:
        model_path = os.path.join(FLAGS.train_dir, ckpt_file)
        model_path2 = os.path.join(FLAGS.train_dir, ckpt_file2)
        if tf.gfile.Exists(model_path) and tf.gfile.Exists(model_path2):
            sys.stderr.write("Reading model parameters from {} and {}\n".format(model_path, model_path2))
            sys.stderr.flush()
            model.saver_old.restore(session, model_path)
            model.saver.restore(session, model_path2)
    else:
        ckpt = tf.train.get_checkpoint_state(FLAGS.train_dir)
        if ckpt and tf.gfile.Exists(ckpt.model_checkpoint_path):
            print("Reading model parameters from %s" % ckpt.model_checkpoint_path)
            model.saver.restore(session, ckpt.model_checkpoint_path)
        else:
            print("Created model with fresh parameters.")
            session.run(tf.initialize_all_variables())
    return model


def train():
    """Train a en->fr translation model using WMT data."""
    print("Preparing training and dev data in %s" % FLAGS.data_dir)
    src_train, trg_train, src_dev, trg_dev, src_vocab_path, trg_vocab_path = data_utils.prepare_wmt_data(
            FLAGS.data_dir, FLAGS.src_vocab_size, FLAGS.trg_vocab_size)

    src_vocab, rev_src_vocab = data_utils.initialize_vocabulary(src_vocab_path)
    trg_vocab, rev_trg_vocab = data_utils.initialize_vocabulary(trg_vocab_path)

    if FLAGS.src_vocab_size > len(src_vocab):
        FLAGS.src_vocab_size = len(src_vocab)
    if FLAGS.trg_vocab_size > len(trg_vocab):
        FLAGS.trg_vocab_size = len(trg_vocab)

    f = open("{}/mems2t.pkl".format(FLAGS.data_dir), 'rb')
    mems2t = pkl.load(f)
    f.close()

    f = open("{}/memt2s.pkl".format(FLAGS.data_dir), 'rb')
    memt2s = pkl.load(f)
    f.close()

    with tf.Session() as sess:
        # Create model.
        print("Creating %d layers of %d units with word embedding %d."
              % (FLAGS.num_layers, FLAGS.hidden_units, FLAGS.hidden_edim))
        model = create_model(sess, False, FLAGS.model, FLAGS.model2)

        # Read data into buckets and compute their sizes.
        dev_set = read_data(src_dev, trg_dev)
        train_set = read_data(src_train, trg_train)
        train_bucket_sizes = [len(train_set[b]) for b in xrange(len(_buckets))]
        train_total_size = float(sum(train_bucket_sizes))

        # A bucket scale is a list of increasing numbers from 0 to 1 that we'll use
        # to select a bucket. Length of [scale[i], scale[i+1]] is proportional to
        # the size if i-th training bucket, as used later.
        train_buckets_scale = [sum(train_bucket_sizes[:i + 1]) / train_total_size
                               for i in xrange(len(train_bucket_sizes))]

        # This is the training loop.
        step_time, loss = 0.0, 0.0
        current_step = 0
        previous_losses = []
        while True:
            # Choose a bucket according to data distribution. We pick a random number
            # in [0, 1] and use the corresponding interval in train_buckets_scale.
            random_number_01 = np.random.random_sample()
            bucket_id = min([i for i in xrange(len(train_buckets_scale))
                             if train_buckets_scale[i] > random_number_01])

            # Get a batch and make a step.
            start_time = time.time()
            encoder_inputs, encoder_mask, encoder_probs, encoder_ids, encoder_hs, mem_mask, decoder_inputs, \
            target_weights, decoder_aligns, decoder_align_weights = model.get_batch(
                    train_set, bucket_id, mems2t, memt2s)

            _, step_loss, _ = model.step(sess, encoder_inputs, encoder_mask, encoder_probs, encoder_ids, encoder_hs, mem_mask,
                                         decoder_inputs, target_weights, decoder_aligns, decoder_align_weights,
                                         bucket_id, False)

            step_time += (time.time() - start_time) / FLAGS.steps_per_checkpoint
            loss += step_loss / FLAGS.steps_per_checkpoint
            current_step += 1

            # Once in a while, we save checkpoint, print statistics, and run evals.
            if current_step % FLAGS.steps_per_checkpoint == 0:
                # Print statistics for the previous epoch.
                perplexity = math.exp(loss) if loss < 300 else float('inf')
                print("global step %d learning rate %.8f step-time %.2f perplexity "
                      "%.2f" % (model.global_step.eval(), model.learning_rate.eval(),
                                step_time, perplexity))

                # Decrease learning rate if no improvement was seen over last 3 times.
                if len(previous_losses) > 2 and loss > max(previous_losses[-3:]):
                    sess.run(model.learning_rate_decay_op)
                previous_losses.append(loss)
                # Save checkpoint and zero timer and loss.
                checkpoint_path = os.path.join(FLAGS.train_dir, "translate.ckpt")
                model.saver.save(sess, checkpoint_path, global_step=model.global_step)
                step_time, loss = 0.0, 0.0
                # Run evals on development set and print their perplexity.
                for bucket_id in xrange(len(_buckets)):
                    if len(dev_set[bucket_id]) == 0:
                        print("  eval: empty bucket %d" % (bucket_id))
                        continue
                    encoder_inputs, encoder_mask, encoder_probs, encoder_ids, encoder_hs, mem_mask, decoder_inputs, \
                    target_weights, decoder_aligns, decoder_align_weights = model.get_batch(
                            dev_set, bucket_id, mems2t, memt2s)
                    _, eval_loss, _ = model.step(sess, encoder_inputs, encoder_mask, encoder_probs, encoder_ids,
                                                 encoder_hs, mem_mask, decoder_inputs, target_weights, decoder_aligns,
                                                 decoder_align_weights, bucket_id, True)
                    eval_ppx = math.exp(eval_loss) if eval_loss < 300 else float('inf')
                    print("  eval: bucket %d perplexity %.2f" % (bucket_id, eval_ppx))  # annotated by yfeng
                sys.stdout.flush()


def decode():
    with tf.Session() as sess:
        # Load vocabularies.
        src_vocab_path = os.path.join(FLAGS.data_dir,
                                     "vocab%d.src" % FLAGS.src_vocab_size)
        trg_vocab_path = os.path.join(FLAGS.data_dir,
                                     "vocab%d.trg" % FLAGS.trg_vocab_size)
        src_vocab, rev_src_vocab = data_utils.initialize_vocabulary(src_vocab_path)
        trg_vocab, rev_trg_vocab = data_utils.initialize_vocabulary(trg_vocab_path)

        if FLAGS.src_vocab_size > len(src_vocab):
            FLAGS.src_vocab_size = len(src_vocab)
        if FLAGS.trg_vocab_size > len(trg_vocab):
            FLAGS.trg_vocab_size = len(trg_vocab)

        f = open("{}/mems2t.pkl".format(FLAGS.data_dir), 'rb')
        mems2t = pkl.load(f)
        f.close()

        f = open("{}/memt2s.pkl".format(FLAGS.data_dir), 'rb')
        memt2s = pkl.load(f)
        f.close()

        # Create model and load parameters.
        model = create_model(sess, True, FLAGS.model, FLAGS.model2)
        model.batch_size = 1  # We decode one sentence at a time.

        sentence = sys.stdin.readline()
        while sentence:
            token_ids = data_utils.sentence_to_token_ids(tf.compat.as_bytes(sentence), src_vocab)
            token_ids.append(data_utils.EOS_ID)
            # Which bucket does it belong to?
            bucket_id = min([b for b in xrange(len(_buckets)) if _buckets[b][0] > len(token_ids)])
            # Get a 1-element batch to feed the sentence to the model.
            encoder_inputs, encoder_mask, encoder_probs, encoder_ids, encoder_hs, mem_mask, decoder_inputs, \
            target_weights, decoder_aligns, decoder_align_weights = model.get_batch(
                    {bucket_id: [(token_ids, [])]}, bucket_id, mems2t, memt2s)
            # Get output logits for the sentence.
            _, _, output_logits = model.step(sess, encoder_inputs, encoder_mask, encoder_probs, encoder_ids,
                                             encoder_hs, mem_mask, decoder_inputs, target_weights, decoder_aligns,
                                             decoder_align_weights, bucket_id, True)

            # This is a beam search decoder - output is the best result from beam search
            outputs = [int(logit) for logit in output_logits]

            # If there is an EOS symbol in outputs, cut them at that point.
            if data_utils.EOS_ID in outputs:
                outputs = outputs[:outputs.index(data_utils.EOS_ID)]
            print(" ".join([tf.compat.as_str(rev_trg_vocab[output]) for output in outputs]))
            sentence = sys.stdin.readline()

def main(_):
    if FLAGS.decode:
        decode()
    else:
        train()


if __name__ == "__main__":
    tf.app.run()
