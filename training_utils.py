import tensorflow as tf
import numpy as np
import eval_utils
import text_utils
import image_utils


class DocumentSequence(tf.keras.utils.Sequence):
    def __init__(self,
                 data_in,
                 image_matrix,
                 image_idx2row,
                 max_sentences_per_doc,
                 max_images_per_doc,
                 vocab,
                 args=None,
                 augment=True,
                 shuffle_sentences=False,
                 shuffle_images=True,
                 shuffle_docs=True):
        self.data_in = data_in
        self.image_matrix = image_matrix
        self.image_idx2row = image_idx2row
        self.max_sentences_per_doc = max_sentences_per_doc
        self.max_images_per_doc = max_images_per_doc
        self.vocab = vocab
        self.args = args
        self.argument = augment
        self.shuffle_sentences = shuffle_sentences
        self.shuffle_images = shuffle_images
        self.shuffle_docs = shuffle_docs

    def __len__(self):
        return int(np.ceil(len(self.data_in) / self.args.docs_per_batch))
    
    def __getitem__(self, idx):
        start = idx * self.args.docs_per_batch
        end = (idx + 1) * self.args.docs_per_batch
        cur_doc_b = self.data_in[start: end]

        if idx == len(self) - 1: # final batch may have wrong number of docs
            docs_to_add = self.args.docs_per_batch - len(cur_doc_b)
            cur_doc_b += self.data_in[:docs_to_add]
        
        images, texts = [], []
        image_n_docs, text_n_docs = [], []
        for idx, vers in enumerate(cur_doc_b):
            cur_images = [img[0] for img in vers[0]]
            cur_text = [text[0] for text in vers[1]]

            if self.shuffle_sentences and not (self.args and self.args.subsample_text > 0):
                np.random.shuffle(cur_text)

            if self.shuffle_images and not (self.args and self.args.subsample_image > 0):
                np.random.shuffle(cur_images)

            if self.args and self.args.subsample_image > 0:
                np.random.shuffle(cur_images)
                cur_images = cur_images[:self.args.subsample_image]

            if self.args and self.args.subsample_text > 0:
                np.random.shuffle(cur_text)
                cur_text = cur_text[:self.args.subsample_text]

            if self.args.end2end:
                cur_images = image_utils.images_to_images(cur_images, augment, args)
                if self.args and self.args.subsample_image > 0:
                    image_padding = np.zeros(
                        (self.args.subsample_image - cur_images.shape[0], 224, 224, 3))
                else:
                    image_padding = np.zeros(
                        (self.max_images_per_doc - cur_images.shape[0], 224, 224, 3))
            else:
                cur_images = image_utils.images_to_matrix(
                    cur_images, self.image_matrix, self.image_idx2row)
                if self.args and self.args.subsample_image > 0:
                    image_padding = np.zeros(
                        (self.args.subsample_image - cur_images.shape[0], cur_images.shape[-1]))
                else:
                    image_padding = np.zeros(
                        (self.max_images_per_doc - cur_images.shape[0], cur_images.shape[-1]))

            cur_text = text_utils.text_to_matrix(cur_text, self.vocab, max_len=self.args.seq_len)
            image_n_docs.append(cur_images.shape[0])
            text_n_docs.append(cur_text.shape[0])

            if self.args and self.args.subsample_text > 0:
                text_padding = np.zeros(
                    (self.args.subsample_text - cur_text.shape[0], cur_text.shape[-1]))
            else:
                text_padding = np.zeros(
                    (self.max_sentences_per_doc - cur_text.shape[0], cur_text.shape[-1]))

            # cudnn cant do empty sequences, so for now, I will just put an UNK in front of all sequences.
            # see comment in text_utils.py for more information.
            text_padding[:, 0] = 1

            cur_images = np.vstack([cur_images, image_padding])
            cur_text = np.vstack([cur_text, text_padding])

            cur_images = np.expand_dims(cur_images, 0)
            cur_text = np.expand_dims(cur_text, 0)

            images.append(cur_images)
            texts.append(cur_text)

        images = np.vstack(images)
        texts = np.vstack(texts)

        image_n_docs = np.expand_dims(np.array(image_n_docs), -1)
        text_n_docs = np.expand_dims(np.array(text_n_docs), -1)

        y = [np.zeros(len(text_n_docs)), np.zeros(len(image_n_docs))]

        return ([texts,
                 images,
                 text_n_docs,
                 image_n_docs], y)

    
    def on_epoch_end(self):
        if self.shuffle_docs:
            np.random.shuffle(self.data_in)


class SaveDocModels(tf.keras.callbacks.Callback):

    def __init__(self,
                 checkpoint_dir,
                 single_text_doc_model,
                 single_image_doc_model):
        super(SaveDocModels, self).__init__()
        self.checkpoint_dir = checkpoint_dir
        self.single_text_doc_model = single_text_doc_model
        self.single_image_doc_model = single_image_doc_model
        
        
    def on_train_begin(self, logs={}):
        self.best_val_loss = np.inf
        self.best_checkpoints_and_logs = None

    def on_epoch_end(self, epoch, logs):
        if logs['val_loss'] < self.best_val_loss:
            print('New best val loss: {:.5f}'.format(logs['val_loss']))
            self.best_val_loss = logs['val_loss']
        else:
            return
        image_model_str = self.checkpoint_dir + '/image_model_epoch_{}_val={:.5f}.model'.format(epoch, logs['val_loss'])
        sentence_model_str = self.checkpoint_dir + '/text_model_epoch_{}_val={:.5f}.model'.format(epoch, logs['val_loss'])
        self.best_checkpoints_and_logs = (image_model_str, sentence_model_str, logs, epoch)

        self.single_text_doc_model.save(sentence_model_str, overwrite=True, save_format='h5')
        self.single_image_doc_model.save(image_model_str, overwrite=True, save_format='h5')



class ReduceLROnPlateauAfterValLoss(tf.keras.callbacks.ReduceLROnPlateau):
    '''
    Delays the normal operation of ReduceLROnPlateau until the validation
    loss reaches a given value.
    '''
    def __init__(self, activation_val_loss=np.inf, *args, **kwargs):
        super(ReduceLROnPlateauAfterValLoss, self).__init__(*args, **kwargs)
        self.activation_val_loss = activation_val_loss
        self.val_threshold_activated = False
        
    def in_cooldown(self):
        if not self.val_threshold_activated: # check to see if we should activate
            if self.current_logs['val_loss'] < self.activation_val_loss:
                print('Current validation loss ({}) less than activation val loss ({})'.
                      format(self.current_logs['val_loss'],
                             self.activation_val_loss))
                print('Normal operation of val LR reduction started.')
                self.val_threshold_activated = True
                self._reset()
        
        return self.cooldown_counter > 0 or not self.val_threshold_activated

    def on_epoch_end(self, epoch, logs=None):
        self.current_logs = logs
        super(ReduceLROnPlateauAfterValLoss, self).on_epoch_end(epoch, logs=logs)
    

class PrintMetrics(tf.keras.callbacks.Callback):
    def __init__(self,
                 val,
                 image_features,
                 image_idx2row,
                 word2idx,
                 single_text_doc_model,
                 single_img_doc_model,
                 args):
        super(PrintMetrics, self).__init__()
        self.val = val
        self.image_features = image_features
        self.image_idx2row = image_idx2row
        self.word2idx = word2idx
        self.single_text_doc_model = single_text_doc_model
        self.single_img_doc_model = single_img_doc_model
        self.args = args
        
    def on_train_begin(self, logs=None):
        self.epoch = []
        self.history = {}

    def on_epoch_end(self, epoch, logs):
        metrics = eval_utils.print_all_metrics(
            self.val,
            self.image_features,
            self.image_idx2row,
            self.word2idx,
            self.single_text_doc_model,
            self.single_img_doc_model,
            self.args)
        self.epoch.append(epoch)
        for k, v in metrics.items():
            self.history.setdefault(k, []).append(v)


class LearningRateLinearIncrease(tf.keras.callbacks.Callback):
    def __init__(self, max_lr, warmup_steps, verbose=0):
        super(LearningRateLinearIncrease, self).__init__()
        self.max_lr = max_lr
        self.warmup_steps = warmup_steps
        self.verbose = verbose
        self.cur_step_count = 0

    def on_train_begin(self, logs=None):
        tf.keras.backend.set_value(self.model.optimizer.lr, 0.0)
        
    def on_batch_begin(self, batch, logs=None):
        if self.cur_step_count >= self.warmup_steps:
            return
        lr = float(tf.keras.backend.get_value(self.model.optimizer.lr))
        lr += 1./self.warmup_steps * self.max_lr
        if self.verbose and self.cur_step_count % 50 == 0:
            print('\n new LR = {}\n'.format(lr))
        tf.keras.backend.set_value(self.model.optimizer.lr, lr)
        self.cur_step_count += 1
