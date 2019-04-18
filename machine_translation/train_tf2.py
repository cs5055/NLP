# -*- coding: utf-8 -*-

import tensorflow as tf
import numpy as np
import unicodedata
import re
import matplotlib.pyplot as plt
import os
import imageio
from zipfile import ZipFile
import requests

# Mode can be either 'train' or 'infer'
# Set to 'infer' will skip the training
MODE = 'train'
URL = 'http://www.manythings.org/anki/fra-eng.zip'
FILENAME = 'fra-eng.zip'
BATCH_SIZE = 64
EMBEDDING_SIZE = 256
LSTM_SIZE = 512
NUM_EPOCHS = 15


def maybe_download_and_read_file(url, filename):
    if not os.path.exists(filename):
        session = requests.Session()
        response = session.get(url, stream=True)

        CHUNK_SIZE = 32768
        with open(filename, "wb") as f:
            for chunk in response.iter_content(CHUNK_SIZE):
                if chunk:
                    f.write(chunk)

    zipf = ZipFile(filename)
    filename = zipf.namelist()
    with zipf.open('fra.txt') as f:
        lines = f.read()

    return lines


lines = maybe_download_and_read_file(URL, FILENAME)
lines = lines.decode('utf-8')

raw_data = []
for line in lines.split('\n'):
    raw_data.append(line.split('\t'))

print(raw_data[-5:])
raw_data = raw_data[:-1]


def unicode_to_ascii(s):
    return ''.join(
        c for c in unicodedata.normalize('NFD', s)
        if unicodedata.category(c) != 'Mn'
    )


def normalize_string(s):
    s = unicode_to_ascii(s)
    s = re.sub(r'([!.?])', r' \1', s)
    s = re.sub(r'[^a-zA-Z.!?]+', r' ', s)
    s = re.sub(r'\s+', r' ', s)
    return s


raw_data_en, raw_data_fr = zip(*raw_data)
raw_data_en = [normalize_string(data) for data in raw_data_en]
raw_data_fr_in = ['<start> ' + normalize_string(data) for data in raw_data_fr]
raw_data_fr_out = [normalize_string(data) + ' <end>' for data in raw_data_fr]

print(raw_data_fr_out[-5:])

en_tokenizer = tf.keras.preprocessing.text.Tokenizer(filters='')
en_tokenizer.fit_on_texts(raw_data_en)
data_en = en_tokenizer.texts_to_sequences(raw_data_en)
data_en = tf.keras.preprocessing.sequence.pad_sequences(data_en,
                                                        padding='post')
print(data_en[:2])

fr_tokenizer = tf.keras.preprocessing.text.Tokenizer(filters='')
fr_tokenizer.fit_on_texts(raw_data_fr_in)
fr_tokenizer.fit_on_texts(raw_data_fr_out)
data_fr_in = fr_tokenizer.texts_to_sequences(raw_data_fr_in)
data_fr_in = tf.keras.preprocessing.sequence.pad_sequences(data_fr_in,
                                                           padding='post')
print(data_fr_in[:2])

data_fr_out = fr_tokenizer.texts_to_sequences(raw_data_fr_out)
data_fr_out = tf.keras.preprocessing.sequence.pad_sequences(data_fr_out,
                                                            padding='post')
print(data_fr_out[:2])

dataset = tf.data.Dataset.from_tensor_slices(
    (data_en, data_fr_in, data_fr_out))
dataset = dataset.shuffle(len(raw_data_en)).batch(
    BATCH_SIZE, drop_remainder=True)


class Encoder(tf.keras.Model):
    def __init__(self, vocab_size, embedding_size, lstm_size):
        super(Encoder, self).__init__()
        self.lstm_size = lstm_size
        self.embedding = tf.keras.layers.Embedding(vocab_size, embedding_size)
        self.lstm = tf.keras.layers.LSTM(
            lstm_size, return_sequences=True, return_state=True)

    def call(self, sequence, states):
        embed = self.embedding(sequence)
        output, state_h, state_c = self.lstm(embed, initial_state=states)

        return output, state_h, state_c

    def init_states(self, batch_size):
        return (tf.zeros([batch_size, self.lstm_size]),
                tf.zeros([batch_size, self.lstm_size]))


en_vocab_size = len(en_tokenizer.word_index) + 1

encoder = Encoder(en_vocab_size, EMBEDDING_SIZE, LSTM_SIZE)

initial_state = encoder.init_states(1)
test_encoder_output = encoder(tf.constant(
    [[1, 23, 4, 5, 0, 0]]), initial_state)
print(test_encoder_output[0].shape)


class Decoder(tf.keras.Model):
    def __init__(self, vocab_size, embedding_size, lstm_size):
        super(Decoder, self).__init__()
        self.lstm_size = lstm_size
        self.embedding = tf.keras.layers.Embedding(vocab_size, embedding_size)
        self.lstm = tf.keras.layers.LSTM(
            lstm_size, return_sequences=True, return_state=True)
        self.dense = tf.keras.layers.Dense(vocab_size)

    def call(self, sequence, state):
        embed = self.embedding(sequence)
        lstm_out, state_h, state_c = self.lstm(embed, state)
        logits = self.dense(lstm_out)

        return logits, state_h, state_c


fr_vocab_size = len(fr_tokenizer.word_index) + 1
decoder = Decoder(fr_vocab_size, EMBEDDING_SIZE, LSTM_SIZE)
de_initial_state = test_encoder_output[1:]

test_decoder_output = decoder(tf.constant(
    [[1, 3, 5, 7, 9, 0, 0, 0]]), de_initial_state)
print(test_decoder_output[0].shape)


def loss_func(targets, logits):
    crossentropy = tf.keras.losses.SparseCategoricalCrossentropy(
        from_logits=True)
    mask = tf.math.logical_not(tf.math.equal(targets, 0))
    mask = tf.cast(mask, dtype=tf.int64)
    loss = crossentropy(targets, logits, sample_weight=mask)

    return loss


optimizer = tf.keras.optimizers.Adam(clipnorm=5.0)


@tf.function
def train_step(source_seq, target_seq_in, target_seq_out, en_initial_states):
    with tf.GradientTape() as tape:
        en_outputs = encoder(source_seq, en_initial_states)
        en_states = en_outputs[1:]
        de_states = en_states

        de_outputs = decoder(target_seq_in, de_states)
        logits = de_outputs[0]
        loss = loss_func(target_seq_out, logits)

    variables = encoder.trainable_variables + decoder.trainable_variables
    gradients = tape.gradient(loss, variables)
    optimizer.apply_gradients(zip(gradients, variables))

    return loss


def predict(test_source_text=None):
    if test_source_text is None:
        test_source_text = raw_data_en[np.random.choice(len(raw_data_en))]
        print(test_source_text)
    test_source_seq = en_tokenizer.texts_to_sequences([test_source_text])
    print(test_source_seq)

    en_initial_states = encoder.init_states(1)
    en_outputs = encoder(tf.constant(test_source_seq), en_initial_states)

    de_input = tf.constant([[fr_tokenizer.word_index['<start>']]])
    de_state_h, de_state_c = en_outputs[1:]
    out_words = []

    while True:
        de_output, de_state_h, de_state_c = decoder(
            de_input, (de_state_h, de_state_c))
        de_input = tf.argmax(de_output, -1)
        out_words.append(fr_tokenizer.index_word[de_input.numpy()[0][0]])

        if out_words[-1] == '<end>' or len(out_words) >= 20:
            break

    print(' '.join(out_words))


if not os.path.exists('checkpoints/encoder'):
    os.makedirs('checkpoints/encoder')
if not os.path.exists('checkpoints/decoder'):
    os.makedirs('checkpoints/decoder')

    
# Uncomment these lines for inference mode
encoder_checkpoint = tf.train.latest_checkpoint('checkpoints/encoder')
decoder_checkpoint = tf.train.latest_checkpoint('checkpoints/decoder')

if encoder_checkpoint is not None and decoder_checkpoint is not None:
    encoder.load_weights(encoder_checkpoint)
    decoder.load_weights(decoder_checkpoint)
    
if MODE == 'train':
    for e in range(NUM_EPOCHS):
        en_initial_states = encoder.init_states(BATCH_SIZE)
        encoder.save_weights('checkpoints/encoder/encoder_{}.h5'.format(e + 1))
        decoder.save_weights('checkpoints/decoder/decoder_{}.h5'.format(e + 1))

        for batch, (source_seq, target_seq_in, target_seq_out) in enumerate(dataset.take(-1)):
            loss = train_step(source_seq, target_seq_in, target_seq_out, en_initial_states)

            if batch % 100 == 0:
                print('Epoch {} Batch {} Loss {:.4f}'.format(e + 1, batch, loss.numpy()))

        try:
            predict()

            predict("How are you today ?")
        except Exception:
            continue

test_sents = (
    'What a ridiculous concept!',
    'Your idea is not entirely crazy.',
    "A man's worth lies in what he is.",
    'What he did is very wrong.',
    "All three of you need to do that.",
    "Are you giving me another chance?",
    "Both Tom and Mary work as models.",
    "Can I have a few minutes, please?",
    "Could you close the door, please?",
    "Did you plant pumpkins this year?",
    "Do you ever study in the library?",
    "Don't be deceived by appearances.",
    "Excuse me. Can you speak English?",
    "Few people know the true meaning.",
    "Germany produced many scientists.",
    "Guess whose birthday it is today.",
    "He acted like he owned the place.",
    "Honesty will pay in the long run.",
    "How do we know this isn't a trap?",
    "I can't believe you're giving up.",
)

for test_sent in test_sents:
    test_sequence = normalize_string(test_sent)
    predict(test_sequence)
