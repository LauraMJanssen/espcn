import os
import logging
import fnmatch
import tensorflow as tf

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
os.environ['CUDA_VISIBLE_DEVICES'] = '0'  # Set a single gpu

from keras.layers import Input, Conv2D, Lambda
from keras.layers import ReLU, Activation
from tensorflow.keras.optimizers import SGD, Adam
from keras.models import Model
from keras.callbacks import TensorBoard, ModelCheckpoint, LambdaCallback
from keras.callbacks import ReduceLROnPlateau, EarlyStopping
from keras.initializers import RandomNormal
from keras_tqdm import TQDMCallback
from tensorflow.keras.utils import OrderedEnqueuer, GeneratorEnqueuer, SequenceEnqueuer

import restore
from util import DataLoader, plot_test_images
from losses import psnr3 as psnr
from losses import euclidean


class ESPCN():
    """
        height_lr: height of the lr image
        width_lr: width of the lr image
        channels: number of channel of the image
        upscaling_factor= factor upscaling
        lr = learning rate
        training_mode: True or False
        colorspace: 'RGB' or 'YCbCr'
    """

    def __init__(self,
                 height_lr=24, width_lr=24, channels=3,
                 upscaling_factor=4, lr=1e-2,
                 training_mode=True,
                 colorspace='RGB'
                 ):

        # Low-resolution image dimensions
        self.height_lr = height_lr
        self.width_lr = width_lr

        # High-resolution image dimensions
        if upscaling_factor not in [2, 4, 8]:
            raise ValueError(
                'Upscaling factor must be either 2, 4, or 8. You chose {}'.format(upscaling_factor))
        self.upscaling_factor = upscaling_factor
        self.height_hr = int(self.height_lr * self.upscaling_factor)
        self.width_hr = int(self.width_lr * self.upscaling_factor)

        # Low-resolution and high-resolution shapes
        self.channels = channels
        self.colorspace = colorspace
        self.shape_lr = (self.height_lr, self.width_lr, self.channels)
        self.shape_hr = (self.height_hr, self.width_hr, self.channels)

        self.loss = "mse"
        self.lr = lr

        self.model = self.build_model()
        self.compile_model(self.model)

    def save_weights(self, filepath):
        """Save the networks weights"""
        self.model.save_weights(
            "{}_{}X.h5".format(filepath, self.upscaling_factor))

    def load_weights(self, weights=None, **kwargs):
        print(">> Loading weights...")
        if weights:
            self.model.load_weights(weights, **kwargs)

    def compile_model(self, model):
        """Compile the srcnn with appropriate optimizer"""

        model.compile(
            loss=self.loss,
            optimizer=Adam(lr=self.lr, beta_1=0.9, beta_2=0.999),
            metrics=[psnr]
        )

    def build_model(self):

        def SubpixelConv2D(scale=4, name="subpixel"):
            def subpixel_shape(input_shape):
                dims = [input_shape[0],
                        None if input_shape[1] is None else input_shape[1] * scale,
                        None if input_shape[2] is None else input_shape[2] * scale,
                        int(input_shape[3] / (scale ** 2))]
                output_shape = tuple(dims)
                return output_shape

            def subpixel(x):
                return tf.compat.v1.depth_to_space(x, scale)

            return Lambda(subpixel, output_shape=subpixel_shape, name=name)

        inputs = Input(shape=(None, None, self.channels), name='input_1')

        x = Conv2D(filters=64, kernel_size=(5, 5), strides=1,
                   padding="same", activation='relu', name='conv_1')(inputs)

        x = Conv2D(filters=32, kernel_size=(3, 3), strides=1,
                   padding="same", activation='relu', name='conv_2')(x)

        x = Conv2D(filters=self.upscaling_factor ** 2 * self.channels, kernel_size=(3, 3), strides=1,
                   padding="same", name='conv_3')(x)

        x = SubpixelConv2D(scale=self.upscaling_factor, name='subpixel_1')(x)

        x = Activation('tanh')(x)

        model = Model(inputs=inputs, outputs=x)
        # model.summary()
        return model

    def train(self,
              epochs=50,
              batch_size=8,
              steps_per_epoch=5,
              steps_per_validation=5,
              crops_per_image=4,
              print_frequency=5,
              log_tensorboard_update_freq=10,
              workers=4,
              max_queue_size=5,
              model_name='ESPCN',
              datapath_train='../../../videos_harmonic/MYANMAR_2160p/train/',
              datapath_validation='../../../videos_harmonic/MYANMAR_2160p/validation/',
              datapath_test='../../../videos_harmonic/MYANMAR_2160p/test/',
              log_weight_path='../model/',
              log_tensorboard_path='../logs/',
              log_test_path='../test/',
              media_type='i'
              ):

        # Create data loaders
        train_loader = DataLoader(
            datapath_train, batch_size,
            self.height_hr, self.width_hr,
            self.upscaling_factor,
            crops_per_image,
            media_type,
            self.channels,
            self.colorspace
        )

        validation_loader = None
        if datapath_validation is not None:
            validation_loader = DataLoader(
                datapath_validation, batch_size,
                self.height_hr, self.width_hr,
                self.upscaling_factor,
                crops_per_image,
                media_type,
                self.channels,
                self.colorspace
            )

        test_loader = None
        if datapath_test is not None:
            test_loader = DataLoader(
                datapath_test, 1,
                self.height_hr, self.width_hr,
                self.upscaling_factor,
                1, media_type,
                self.channels, self.colorspace
            )

        # Callback: tensorboard
        callbacks = []
        if log_tensorboard_path:
            tensorboard = TensorBoard(
                log_dir=os.path.join(log_tensorboard_path, model_name),
                histogram_freq=0,
                batch_size=batch_size,
                write_graph=True,
                write_grads=True,
                update_freq=log_tensorboard_update_freq
            )
            callbacks.append(tensorboard)
        else:
            print(">> Not logging to tensorboard since no log_tensorboard_path is set")

        # Callback: Stop training when a monitored quantity has stopped improving
        earlystopping = EarlyStopping(
            monitor='val_loss',
            patience=100, verbose=1,
            restore_best_weights=True)
        callbacks.append(earlystopping)

        # Callback: Reduce lr when a monitored quantity has stopped improving
        reduce_lr = ReduceLROnPlateau(monitor='val_loss', factor=0.5,
                                      patience=100, min_lr=1e-4, verbose=1)
        callbacks.append(reduce_lr)

        # Callback: save weights after each epoch
        modelcheckpoint = ModelCheckpoint(
            os.path.join(log_weight_path, model_name + '_{}X.h5'.format(self.upscaling_factor)),
            monitor='val_loss',
            save_best_only=True,
            save_weights_only=True)
        callbacks.append(modelcheckpoint)

        # Callback: test images plotting
        if datapath_test is not None:
            testplotting = LambdaCallback(
                on_epoch_end=lambda epoch, logs: None if ((epoch + 1) % print_frequency != 0) else plot_test_images(
                    self.model,
                    test_loader,
                    datapath_test,
                    log_test_path,
                    epoch + 1,
                    name=model_name,
                    channels=self.channels,
                    colorspace=self.colorspace))
        callbacks.append(testplotting)

        # Use several workers on CPU for preparing batches
        enqueuer = OrderedEnqueuer(
            train_loader,
            use_multiprocessing=True
        )
        enqueuer.start(workers=workers, max_queue_size=max_queue_size)
        output_generator = enqueuer.get()

        self.model.fit_generator(
            output_generator,
            steps_per_epoch=steps_per_epoch,
            epochs=epochs,
            validation_data=validation_loader,
            validation_steps=steps_per_validation,
            callbacks=callbacks,
            # shuffle=True,
            use_multiprocessing=False,  # workers>1
            workers=1  # workers
        )

    def predict(self,
                lr_path=None,
                sr_path=None,
                print_frequency=False,
                qp=8,
                fps=None,
                media_type=None,
                gpu=False
                ):
        """ lr_videopath: path of video in low resoluiton
            sr_videopath: path to output video
            print_frequency: print frequncy the time per frame and estimated time, if False no print
            crf: [0,51] QP parameter 0 is the best quality and 51 is the worst one
            fps: framerate if None is use the same framerate of the LR video
            media_type: type of media 'v' to video and 'i' to image
        """
        if (media_type == 'v'):
            time_elapsed = restore.write_srvideo(self.model, lr_path, sr_path, self.upscaling_factor,
                                                 print_frequency=print_frequency, crf=qp, fps=fps, gpu=gpu)
        elif (media_type == 'i'):
            time_elapsed = restore.write_sr_images(self.model, lr_imagepath=lr_path, sr_imagepath=sr_path,
                                                   scale=self.upscaling_factor)
        else:
            print(">> Media type not defined or not suported!")
            return 0
        return time_elapsed


def restoration(resolution=None, k=1, qp='25'):
    logging.basicConfig(filename='/content/espcn/logs/espcn.log', level=logging.INFO)
    logging.info('Started')
    # ------------------------------------------------------

    # Instantiate the TSRGAN object
    logging.info(">> Creating the ESPCN network")
    # Instantiate the ESPCN object
    espcn = ESPCN(height_lr=17, width_lr=17, channels=3, lr=1e-4, upscaling_factor=4, colorspace='RGB')
    espcn.load_weights(weights='/content/espcn/model/ESPCN_4X.h5')

    if (resolution == None):
        datapath = '/content/drive/MyDrive/IVUSImages/LR_Scale4_Valid/'
        outpath = '/content/results/'
        lfilenames = []
        for dirpath, _, filenames in os.walk(datapath):
            lfilenames = [os.path.join(dirpath, f) for f in filenames if any(
                filetype in f.lower() for filetype in ['jpeg', 'png', 'jpg', 'mp4', '264', 'webm', 'wma'])]
        i = 1
        for filename in sorted(lfilenames):
            if (i >= k):
                print("i={} - {} {}".format(i, filename, outpath + filename.split('/')[-1].split('.')[0] + '.mp4'))
                t = espcn.predict(
                    lr_path=filename,
                    sr_path=outpath + filename.split('/')[-1].split('.')[0] + '.png',
                    qp=i - 1,
                    media_type='i',
                    gpu=False
                )
            i += 1

    if (resolution == '540p'):
        datapath = '/media/joao/SAMSUNG2/videoset_compress/540p/'  # '../../data/videoset/540p/'
        outpath = '/media/joao/SAMSUNG1/data/CISRDCNN-keras/out/540p_2X_qp25/'  # '../out/540p_2X/'
        lfilenames = []
        for dirpath, _, filenames in os.walk(datapath):
            item = [f for f in filenames if
                    any(filetype in f.lower() for filetype in ['jpeg', 'png', 'jpg', 'mp4', '264', 'webm', 'wma']) if
                    fnmatch.fnmatch(f, '*qp_' + str(qp) + '.264')]
            if len(item):
                lfilenames.append(os.path.join(dirpath, item[0]))
        i = 1
        for filename in sorted(lfilenames):
            if (i >= k):
                print("i={} - {} {}".format(i, filename, outpath + filename.split('/')[-1].split('.')[0] + '.mp4'))
                t = espcn.predict(
                    lr_path=filename,
                    sr_path=outpath + filename.split('/')[-1].split('.')[0] + '.mp4',
                    qp=0,
                    media_type='v',
                    gpu=False
                )
            i += 1

    if (resolution == '360p'):
        datapath = '/media/joao/SAMSUNG2/videoset_compress/360p/'  # '../../data/videoset/360p/'
        outpath = '/media/joao/SAMSUNG1/data/CISRDCNN-keras/out/360p_2X_qp25/'  # '../out/360p_2X/'
        i = 1
        for dirpath, _, filenames in os.walk(datapath):
            filenames = sorted([f for f in filenames if any(
                filetype in f.lower() for filetype in ['jpeg', 'png', 'jpg', 'mp4', '264', 'webm', 'wma']) if
                                fnmatch.fnmatch(f, '*qp_' + str(qp) + '.264')])
            for filename in filenames:
                if (i >= k):
                    print("i={} - {}".format(i, os.path.join(dirpath, filename),
                                             outpath + filename.split('.')[0] + '.mp4'))
                    t = espcn.predict(
                        lr_path=os.path.join(dirpath, filename),
                        sr_path=outpath + filename.split('.')[0] + '.mp4',
                        qp=0,
                        media_type='v',
                        gpu=False
                    )
                i += 1

                # ------------------------------------------------------

    logging.info('Finished')


# Run the ESPCN network
if __name__ == "__main__":
    restoration()


