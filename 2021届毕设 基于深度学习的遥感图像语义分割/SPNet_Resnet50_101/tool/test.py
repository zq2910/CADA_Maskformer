import os
import time
import logging
import argparse
# os.environ["CUDA_VISIBLE_DEVICES"] = '0'
import cv2
import numpy as np
import torch
import torch.backends.cudnn as cudnn
import torch.nn.functional as F
import torch.nn.parallel
import torch.utils.data

from util import dataset, transform, config
from util.util import AverageMeter, intersectionAndUnion, check_makedirs, colorize
from tqdm import tqdm

cv2.ocl.setUseOpenCL(False)

class_index = np.array([7, 8, 11, 12, 13, 17, 19, 20, 21, 22,
                                  23, 24, 25, 26, 27, 28, 31, 32, 33])

def get_parser():
    parser = argparse.ArgumentParser(description='PyTorch Semantic Segmentation')
    parser.add_argument('--config', type=str, default='/f2020/shengyifan/network/SPNet/config/postdam/postdam_spnet101.yaml', help='config file')
    parser.add_argument('opts', help='see config/ade20k/ade20k_pspnet50.yaml for all options', default=None, nargs=argparse.REMAINDER)
    args = parser.parse_args()
    assert args.config is not None
    cfg = config.load_cfg_from_cfg_file(args.config)
    if args.opts is not None:
        cfg = config.merge_cfg_from_list(cfg, args.opts)
    return cfg


def get_logger():
    logger_name = "main-logger"
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    fmt = "[%(asctime)s %(levelname)s %(filename)s line %(lineno)d %(process)d] %(message)s"
    handler.setFormatter(logging.Formatter(fmt))
    logger.addHandler(handler)
    return logger


def check(args):
    assert args.classes > 1
    assert args.zoom_factor in [1, 2, 4, 8]
    assert args.split in ['train', 'val', 'test']
    if args.arch == 'psp':
        assert (args.train_h - 1) % 8 == 0 and (args.train_w - 1) % 8 == 0
    elif args.arch == 'psa':
        if args.compact:
            args.mask_h = (args.train_h - 1) // (8 * args.shrink_factor) + 1
            args.mask_w = (args.train_w - 1) // (8 * args.shrink_factor) + 1
        else:
            assert (args.mask_h is None and args.mask_w is None) or (args.mask_h is not None and args.mask_w is not None)
            if args.mask_h is None and args.mask_w is None:
                args.mask_h = 2 * ((args.train_h - 1) // (8 * args.shrink_factor) + 1) - 1
                args.mask_w = 2 * ((args.train_w - 1) // (8 * args.shrink_factor) + 1) - 1
            else:
                assert (args.mask_h % 2 == 1) and (args.mask_h >= 3) and (
                        args.mask_h <= 2 * ((args.train_h - 1) // (8 * args.shrink_factor) + 1) - 1)
                assert (args.mask_w % 2 == 1) and (args.mask_w >= 3) and (
                        args.mask_w <= 2 * ((args.train_h - 1) // (8 * args.shrink_factor) + 1) - 1)
    else:
        raise Exception('architecture not supported yet'.format(args.arch))


def main():
    global args, logger
    args = get_parser()
    #check(args)
    logger = get_logger()
    os.environ["CUDA_VISIBLE_DEVICES"] = ','.join(str(x) for x in args.test_gpu)
    logger.info(args)
    logger.info("=> creating model ...")
    logger.info("Classes: {}".format(args.classes))

    value_scale = 255
    mean = [0.485, 0.456, 0.406]
    mean = [item * value_scale for item in mean]
    std = [0.229, 0.224, 0.225]
    std = [item * value_scale for item in std]

    gray_folder = os.path.join(args.save_folder, 'gray')
    # gray_folder = '/f2020/shengyifan/data/gray/SPNet'
    color_folder = os.path.join(args.save_folder, 'color')
    # color_folder = '/f2020/shengyifan/data/color/SPNet'
    score_folder = os.path.join(args.save_folder, 'score')
    if not os.path.exists(gray_folder):
        os.makedirs(gray_folder)

    if not os.path.exists(color_folder):
        os.makedirs(color_folder)

    if not os.path.exists(score_folder):
        os.makedirs(score_folder)
    test_transform = transform.Compose([transform.ToTensor()])
    if args.dataset == 'ade20k':
        test_data = dataset.Ade20kData(split=args.split, data_root=args.data_root, \
                data_list=args.test_list, transform=test_transform)
    elif args.dataset == 'cityscapes':
        test_data = dataset.CityscapesData(split=args.split, data_root=args.data_root, \
                data_list=args.test_list, transform=test_transform)
    elif args.dataset == 'postdam':
        test_data = dataset.PostdamData(split=args.split, data_root=args.data_root, \
                data_list=args.test_list, transform=test_transform)
    else:
        raise RuntimeError("=> Unsupported dataset: '{}'".format(args.dataset))

    index_start = args.index_start
    if args.index_step == 0:
        index_end = len(test_data.data_list)
    else:
        index_end = min(index_start + args.index_step, len(test_data.data_list))
    test_data.data_list = test_data.data_list[index_start:index_end]
    test_loader = torch.utils.data.DataLoader(test_data, batch_size=1, shuffle=False, num_workers=args.workers, pin_memory=True)
    colors = np.loadtxt(args.colors_path).astype('uint8')
    names = [line.rstrip('\n') for line in open(args.names_path)]


    if not args.has_prediction:
        #if args.arch == 'psp':
        #    from model.pspnet import PSPNet
        #    model = PSPNet(layers=args.layers, classes=args.classes, zoom_factor=args.zoom_factor, pretrained=False)
        #elif args.arch == 'psa':
        #    from model.psanet import PSANet
        #    model = PSANet(layers=args.layers, classes=args.classes, zoom_factor=args.zoom_factor, compact=args.compact,
        #                   shrink_factor=args.shrink_factor, mask_h=args.mask_h, mask_w=args.mask_w,
        #                   normalization_factor=args.normalization_factor, psa_softmax=args.psa_softmax,
        #                   pretrained=False)
        if args.arch == 'spnet':
            from models.spnet import SPNet
            model = SPNet(nclass=args.classes, backbone=args.backbone, pretrained=None, spm_on=args.spm_on)
        elif args.arch == 'htnet':
            from models.htnet import HTNet
            model = HTNet(nclass=args.classes, backbone=args.backbone, pretrained=None)
        # logger.info(model)
        model = torch.nn.DataParallel(model).cuda()
        cudnn.benchmark = True
        if os.path.isfile(args.model_path):
            logger.info("=> loading checkpoint '{}'".format(args.model_path))
            checkpoint = torch.load(args.model_path, map_location='cuda:0')
            model.load_state_dict(checkpoint['state_dict'], strict=False)
            print('epoch:{}'.format(checkpoint['epoch']))
            logger.info("=> loaded checkpoint '{}'".format(args.model_path))
        else:
            raise RuntimeError("=> no checkpoint found at '{}'".format(args.model_path))
        # concise_test(test_data, test_loader, model, mean, std, gray_folder, color_folder, score_folder)
        test(test_loader, test_data, model, args.classes, mean, std, args.base_size, args.test_h, args.test_w, args.scales, gray_folder, color_folder, score_folder, colors)
        # 进行测试
    if args.split != 'test':
        cal_acc(test_data, gray_folder, args.classes, names, args)


def net_process(model, image, mean, std=None, flip=True):
    input = torch.from_numpy(image.transpose((2, 0, 1))).float()
    if std is None:
        for t, m in zip(input, mean):
            t.sub_(m)
    else:
        for t, m, s in zip(input, mean, std):
            t.sub_(m).div_(s)
    input = input.unsqueeze(0).cuda()
    if flip:
        input = torch.cat([input, input.flip(3)], 0)
    with torch.no_grad():
        output = model(input)
    _, _, h_i, w_i = input.shape
    _, _, h_o, w_o = output.shape
    if (h_o != h_i) or (w_o != w_i):
        output = F.interpolate(output, (h_i, w_i), mode='bilinear', align_corners=True)
    output = F.softmax(output, dim=1)
    if flip:
        output = (output[0] + output[1].flip(2)) / 2
    else:
        output = output[0]
    output = output.data.cpu().numpy()
    output = output.transpose(1, 2, 0)
    return output


def scale_process(model, image, classes, crop_h, crop_w, h, w, mean, std=None, stride_rate=2/3):
    ori_h, ori_w, _ = image.shape
    pad_h = max(crop_h - ori_h, 0)
    pad_w = max(crop_w - ori_w, 0)
    pad_h_half = int(pad_h / 2)
    pad_w_half = int(pad_w / 2)
    if pad_h > 0 or pad_w > 0:
        image = cv2.copyMakeBorder(image, pad_h_half, pad_h - pad_h_half, pad_w_half, pad_w - pad_w_half, cv2.BORDER_CONSTANT, value=mean)

    new_h, new_w, _ = image.shape
    stride_h = int(np.ceil(crop_h*stride_rate))
    stride_w = int(np.ceil(crop_w*stride_rate))
    grid_h = int(np.ceil(float(new_h-crop_h)/stride_h) + 1)
    grid_w = int(np.ceil(float(new_w-crop_w)/stride_w) + 1)
    prediction_crop = np.zeros((new_h, new_w, classes), dtype=float)
    count_crop = np.zeros((new_h, new_w), dtype=float)
    for index_h in range(0, grid_h):
        for index_w in range(0, grid_w):
            s_h = index_h * stride_h
            e_h = min(s_h + crop_h, new_h)
            s_h = e_h - crop_h
            s_w = index_w * stride_w
            e_w = min(s_w + crop_w, new_w)
            s_w = e_w - crop_w
            image_crop = image[s_h:e_h, s_w:e_w].copy()
            count_crop[s_h:e_h, s_w:e_w] += 1
            prediction_crop[s_h:e_h, s_w:e_w, :] += net_process(model, image_crop, mean, std)
    prediction_crop /= np.expand_dims(count_crop, 2)
    prediction_crop = prediction_crop[pad_h_half:pad_h_half+ori_h, pad_w_half:pad_w_half+ori_w]
    prediction = cv2.resize(prediction_crop, (w, h), interpolation=cv2.INTER_LINEAR)
    return prediction


def concise_test(test_data, test_loader, model, mean, std, gray_folder, color_folder, score_folder):
    test_RGBdir = './testRGB'
    colors = np.loadtxt('../dataset/postdam/postdam_colors.txt').astype('uint8')
    print('共有{}种颜色'.format(len(colors)))
    data_list = test_data.data_list
    tbar = tqdm(test_loader)
    average_time = []
    model.eval()
    for i, (input, target) in enumerate(tbar):
        with torch.no_grad():
            input = input.squeeze(0)
            if std is None:
                for t, m in zip(input, mean):
                    t.sub_(m)
            else:
                for t, m, s in zip(input, mean, std):
                    t.sub_(m).div_(s)
            input = input.unsqueeze(0).cuda()
            start = time.time()
            outputs = model(input)
            use_time = time.time() - start
            average_time.append(use_time)
            outputs = F.softmax(outputs, dim=1)
            outputs = outputs.squeeze(0)
            prediction = np.argmax(outputs.cpu(), axis=0)
            score = np.max(outputs.cpu().numpy(), axis=0)
            gray = np.uint8(prediction)
            color = colorize(gray, colors)
            image_path, _ = data_list[i]
            # image_path=top_potsdam_2_10_rgb-0.png
            image_name = image_path.split('/')[-1].split('.')[0]
            # image_name=top_potsdam_2_10_rgb-0
            color_path = os.path.join(color_folder, image_name + '_color.png')
            gray_path = os.path.join(gray_folder, image_name + '_pred.png')
            score_path = os.path.join(score_folder, image_name + '_pred_score.npy')
            color.save(color_path)
            cv2.imwrite(gray_path, gray)
            np.save(score_path, score)

    print('Finish!')
    print('Average_time:{}'.format(sum(average_time) / (len(average_time))))


def test(test_loader, test_data, model, classes, mean, std, base_size, crop_h, crop_w, scales, gray_folder, color_folder, score_folder, colors):
    logger.info('>>>>>>>>>>>>>>>> Start Evaluation >>>>>>>>>>>>>>>>')
    data_time = AverageMeter()
    batch_time = AverageMeter()
    model.eval() # 测试模式
    data_list = test_data.data_list
    end = time.time()
    tbar = tqdm(test_loader)
    for i, (input, _) in enumerate(tbar):
        data_time.update(time.time() - end)
        input = np.squeeze(input.numpy(), axis=0)
        image = np.transpose(input, (1, 2, 0))
        h, w, _ = image.shape
        prediction = np.zeros((h, w, classes), dtype=float)
        for scale in scales:
            long_size = round(scale * base_size)
            new_h = long_size
            new_w = long_size
            if h > w:
                new_w = round(long_size/float(h)*w)
            else:
                new_h = round(long_size/float(w)*h)
            image_scale = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
            prediction += scale_process(model, image_scale, classes, crop_h, crop_w, h, w, mean, std)
        prediction /= len(scales)
        prediction_score = np.max(prediction, axis=2)
        prediction = np.argmax(prediction, axis=2)
        batch_time.update(time.time() - end)
        end = time.time()
        # if ((i + 1) % 10 == 0) or (i + 1 == len(test_loader)):
        #     logger.info('Test: [{}/{}] '
        #                 'Data {data_time.val:.3f} ({data_time.avg:.3f}) '
        #                 'Batch {batch_time.val:.3f} ({batch_time.avg:.3f}).'.format(i + 1, len(test_loader),
        #                                                                             data_time=data_time,
        #                                                                             batch_time=batch_time))
        check_makedirs(gray_folder)
        check_makedirs(color_folder)
        check_makedirs(score_folder)
        gray = np.uint8(prediction)
        #gray = class_index[gray]
        color = colorize(gray, colors)
        image_path, _ = data_list[i]
        # image_path=top_potsdam_2_10_rgb-0.png
        image_name = image_path.split('/')[-1].split('.')[0]
        # image_name=top_potsdam_2_10_rgb-0
        gray_path = os.path.join(gray_folder, image_name + '_pred.png')
        color_path = os.path.join(color_folder, image_name + '_color.png')
        score_path = os.path.join(score_folder, image_name + '_pred_score.npy')
        cv2.imwrite(gray_path, gray)
        color.save(color_path)
        np.save(score_path, prediction_score)
    logger.info('<<<<<<<<<<<<<<<<< End Evaluation <<<<<<<<<<<<<<<<<')


def cal_acc(test_data, pred_folder, classes, names, arg):
    intersection_meter = AverageMeter()
    union_meter = AverageMeter()
    target_meter = AverageMeter()
    pred_meter = AverageMeter()
    data_list = test_data.data_list
    for i, (image_path, target_path) in enumerate(data_list):
        image_name = image_path.split('/')[-1].split('.')[0]
        # image_name=top_potsdam_2_10_rgb-0
        pred = cv2.imread(os.path.join(pred_folder, image_name+'_pred.png'), cv2.IMREAD_GRAYSCALE)
        target = cv2.imread(os.path.join(arg.data_root, arg.split, 'gtblack', target_path), cv2.IMREAD_GRAYSCALE)
        target = np.array(target, dtype=np.int32)
        #target -= 1
        # target = test_data._class_to_index(target)
        # target[target < 0] = 255
        intersection, union, target, pred = intersectionAndUnion(pred, target, classes)
        intersection_meter.update(intersection)
        union_meter.update(union)
        target_meter.update(target)
        pred_meter.update(pred)
        accuracy = sum(intersection_meter.val) / (sum(target_meter.val) + 1e-10)
        # logger.info('Evaluating {0}/{1} on image {2}, accuracy {3:.4f}.'.format(i + 1, len(data_list), image_name+'.png', accuracy))


    iou_class = intersection_meter.sum / (union_meter.sum + 1e-10)
    accuracy_class = intersection_meter.sum / (target_meter.sum + 1e-10)
    mIoU = np.mean(iou_class)
    mAcc = np.mean(accuracy_class)
    allAcc = sum(intersection_meter.sum) / (sum(target_meter.sum) + 1e-10)

    po = sum(intersection_meter.sum) / sum(target_meter.sum)
    pe = sum(np.multiply(pred_meter.sum, target_meter.sum)) / sum(target_meter.sum) ** 2
    kappa = (po - pe) / (1 - pe)

    logger.info('Eval result: mIoU/kappa/allAcc {:.4f}/{:.4f}/{:.4f}.'.format(mIoU, kappa, allAcc))
    for i in range(classes):
        logger.info('Class_{} result: iou/accuracy {:.4f}/{:.4f}, name: {}.'.format(i, iou_class[i], accuracy_class[i], names[i]))


if __name__ == '__main__':
    main()
