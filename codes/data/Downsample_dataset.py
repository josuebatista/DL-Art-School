import random
import numpy as np
import cv2
import lmdb
import torch
import torch.utils.data as data
import data.util as util
from PIL import Image
from io import BytesIO
import torchvision.transforms.functional as F


class DownsampleDataset(data.Dataset):
    """
    Reads an unpaired HQ and LQ image. Clips both images to the expected input sizes of the model. Produces a
    downsampled LQ image from the HQ image and feeds that as well.
    """

    def __init__(self, opt):
        super(DownsampleDataset, self).__init__()
        self.opt = opt
        self.data_type = self.opt['data_type']
        self.paths_LQ, self.paths_GT = None, None
        self.sizes_LQ, self.sizes_GT = None, None
        self.LQ_env, self.GT_env = None, None  # environments for lmdb
        self.doCrop = self.opt['doCrop']

        self.paths_GT, self.sizes_GT = util.get_image_paths(self.data_type, opt['dataroot_GT'])
        self.paths_LQ, self.sizes_LQ = util.get_image_paths(self.data_type, opt['dataroot_LQ'])

        self.data_sz_mismatch_ok = opt['mismatched_Data_OK']
        assert self.paths_GT, 'Error: GT path is empty.'
        assert self.paths_LQ, 'LQ is required for downsampling.'
        if not self.data_sz_mismatch_ok:
            assert len(self.paths_LQ) == len(
                self.paths_GT
            ), 'GT and LQ datasets have different number of images - {}, {}.'.format(
                len(self.paths_LQ), len(self.paths_GT))
        self.random_scale_list = [1]

    def _init_lmdb(self):
        # https://github.com/chainer/chainermn/issues/129
        self.GT_env = lmdb.open(self.opt['dataroot_GT'], readonly=True, lock=False, readahead=False,
                                meminit=False)
        self.LQ_env = lmdb.open(self.opt['dataroot_LQ'], readonly=True, lock=False, readahead=False,
                                meminit=False)

    def __getitem__(self, index):
        if self.data_type == 'lmdb' and (self.GT_env is None or self.LQ_env is None):
            self._init_lmdb()
        scale = self.opt['scale']
        GT_size = self.opt['target_size'] * scale

        # get GT image
        GT_path = self.paths_GT[index % len(self.paths_GT)]
        resolution = [int(s) for s in self.sizes_GT[index].split('_')
                      ] if self.data_type == 'lmdb' else None
        img_GT = util.read_img(self.GT_env, GT_path, resolution)
        if self.opt['phase'] != 'train':  # modcrop in the validation / test phase
            img_GT = util.modcrop(img_GT, scale)
        if self.opt['color']:  # change color space if necessary
            img_GT = util.channel_convert(img_GT.shape[2], self.opt['color'], [img_GT])[0]

        # get LQ image
        LQ_path = self.paths_LQ[index % len(self.paths_LQ)]
        resolution = [int(s) for s in self.sizes_LQ[index].split('_')
                      ] if self.data_type == 'lmdb' else None
        img_LQ = util.read_img(self.LQ_env, LQ_path, resolution)

        if self.opt['phase'] == 'train':
            H, W, _ = img_GT.shape
            assert H >= GT_size and W >= GT_size

            H, W, C = img_LQ.shape
            LQ_size = GT_size // scale

            if self.doCrop:
                # randomly crop
                rnd_h = random.randint(0, max(0, H - LQ_size))
                rnd_w = random.randint(0, max(0, W - LQ_size))
                img_LQ = img_LQ[rnd_h:rnd_h + LQ_size, rnd_w:rnd_w + LQ_size, :]
                rnd_h_GT, rnd_w_GT = int(rnd_h * scale), int(rnd_w * scale)
                img_GT = img_GT[rnd_h_GT:rnd_h_GT + GT_size, rnd_w_GT:rnd_w_GT + GT_size, :]
            else:
                img_LQ = cv2.resize(img_LQ, (LQ_size, LQ_size), interpolation=cv2.INTER_LINEAR)
                img_GT = cv2.resize(img_GT, (GT_size, GT_size), interpolation=cv2.INTER_LINEAR)

            # augmentation - flip, rotate
            img_LQ, img_GT = util.augment([img_LQ, img_GT], self.opt['use_flip'],
                                          self.opt['use_rot'])

        # BGR to RGB, HWC to CHW, numpy to tensor
        if img_GT.shape[2] == 3:
            img_GT = cv2.cvtColor(img_GT, cv2.COLOR_BGR2RGB)
            img_LQ = cv2.cvtColor(img_LQ, cv2.COLOR_BGR2RGB)

        # HQ needs to go to a PIL image to perform the compression-artifact transformation.
        H, W, _ = img_GT.shape
        img_GT = (img_GT * 255).astype(np.uint8)
        img_GT = Image.fromarray(img_GT)
        if self.opt['use_compression_artifacts']:
            qf = random.randrange(15, 100)
            corruption_buffer = BytesIO()
            img_GT.save(corruption_buffer, "JPEG", quality=qf, optimice=True)
            corruption_buffer.seek(0)
            img_GT = Image.open(corruption_buffer)
        # Generate a downsampled image from HQ for feature and PIX losses.
        img_Downsampled = F.resize(img_GT, (int(H / scale), int(W / scale)))

        img_GT = F.to_tensor(img_GT)
        img_Downsampled = F.to_tensor(img_Downsampled)
        img_LQ = torch.from_numpy(np.ascontiguousarray(np.transpose(img_LQ, (2, 0, 1)))).float()

        # This may seem really messed up, but let me explain:
        #  The goal is to re-use existing code as much as possible. SRGAN_model was coded to supersample, not downsample,
        #  but it can be retrofitted. To do so, we need to "trick" it. In this case the "input" is the HQ image and the
        #  "output" is the LQ image. SRGAN_model will be using a Generator and a Discriminator which already know this,
        #  we just need to trick its logic into following this rules.
        #  Do this by setting LQ(which is the input into the models)=img_GT and GT(which is the expected outpuut)=img_LQ.
        #  PIX is used as a reference for the pixel loss. Use the manually downsampled image for this.
        return {'LQ': img_GT, 'GT': img_LQ, 'PIX': img_Downsampled, 'LQ_path': LQ_path, 'GT_path': GT_path}

    def __len__(self):
        return max(len(self.paths_GT), len(self.paths_LQ))
