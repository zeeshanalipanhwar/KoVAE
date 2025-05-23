import torch.utils.data as Data
import torch.nn.init
import numpy as np
import tensorflow as tf
import neptune.new as neptune
import random
import argparse

import os, sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.kovae import KoVAE
import torch.optim as optim
import logging
from utils.utils_data import real_data_loading, sine_data_generation
from utils.utils import agg_losses, log_losses, set_seed_device


def define_args():
    parser = argparse.ArgumentParser(description="KoVAE")

    # general
    parser.add_argument('--epochs', type=int, default=600)
    parser.add_argument('--lr', type=float, default=7e-4)
    parser.add_argument('--weight_decay', type=float, default=0.)
    parser.add_argument('--seed', type=int, default=10)
    parser.add_argument('--pinv_solver', type=bool, default=False)
    parser.add_argument('--neptune', default='debug', help='async runs as usual, debug prevents logging')
    parser.add_argument('--tag', default='sine')

    # data
    parser.add_argument("--dataset", default='sine')
    parser.add_argument('--batch_size', type=int, default=64, metavar='N')
    parser.add_argument('--seq_len', type=int, default=24, metavar='N')
    parser.add_argument('--missing_value', type=float, default=0.)

    # model
    parser.add_argument('--batch_norm', type=bool, default=True)
    parser.add_argument('--num_layers', type=int, default=3)
    parser.add_argument('--z_dim', type=int, default=16)
    parser.add_argument('--hidden_dim', type=int, default=20,
                        help='the hidden dimension of the output decoder lstm')

    # loss params
    parser.add_argument('--num_steps', type=int, default=1)
    parser.add_argument('--w_rec', type=float, default=1.)
    parser.add_argument('--w_kl', type=float, default=.007)
    parser.add_argument('--w_pred_prior', type=float, default=0.005)

    return parser

def set_seed_device(seed):
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    torch.manual_seed(seed)
    np.random.seed(seed)
    tf.keras.utils.set_random_seed(seed)

    # Use cuda if available
    if torch.cuda.is_available():
        device = torch.device("cuda:0")
        print('cuda is available')
    else:
        device = torch.device("cpu")
    return device

parser = define_args()
args = parser.parse_args()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


def main(args):

    args.device = set_seed_device(args.seed)
    args.log_dir = './logs'

    name = 'KOVAE-{}_bs={}-rnn_size={}-z_dim={}-lr={}-n_layers={}=' \
           '-weight:kl={}-pred={}-w_decay={}-seed={}'.format(
        args.epochs, args.batch_size, args.hidden_dim, args.z_dim, args.lr, args.num_layers,
        args.w_kl, args.w_pred_prior, args.weight_decay, args.seed)

    args.log_dir = '%s/%s/%s' % (args.log_dir, args.dataset, name)

    # data parameters

    if args.dataset in ["sine"]:
        args.inp_dim = 5
        no, dim = 10000, 5
        ori_data = sine_data_generation(no, args.seq_len, dim)
        ori_data = torch.Tensor(np.array(ori_data))
        args.inp_dim = ori_data.shape[-1]
        train_set = Data.TensorDataset(ori_data)
    else:
        ori_data = real_data_loading(args.dataset, args.seq_len)
        ori_data = torch.Tensor(np.array(ori_data))
        args.inp_dim = ori_data.shape[-1]
        train_set = Data.TensorDataset(ori_data)


    def seed_worker(worker_id):
        worker_seed = torch.initial_seed() % 2 ** 32
        np.random.seed(worker_seed)
        random.seed(worker_seed)

    g = torch.Generator()
    g.manual_seed(args.seed)

    train_loader = Data.DataLoader(dataset=train_set, batch_size=args.batch_size, shuffle=True, num_workers=4,
                                   worker_init_fn=seed_worker, generator=g)

    logging.info(args.dataset + ' dataset is ready.')

    # create model
    model = KoVAE(args).to(device=args.device)

    # optimizer
    optimizer = optim.Adam(model.parameters(), args.lr, weight_decay=args.weight_decay)
    tf.io.gfile.makedirs(os.path.dirname(args.log_dir))

    params_num = sum(param.numel() for param in model.parameters())

    logging.info(args)
    logging.info("number of model parameters: {}".format(params_num))
    print("number of model parameters: {}".format(params_num))

    logging.info("Starting training loop at step %d." % (0,))

    for epoch in range(0, args.epochs):
        logging.info("Running Epoch : {}".format(epoch + 1))

        model.train()
        losses_agg_tr = []
        for i, data in enumerate(train_loader, 1):
            X = data[0].to(args.device)

            optimizer.zero_grad()
            x_rec, Z_enc, Z_enc_prior = model(X)

            losses = model.loss(X, x_rec, Z_enc, Z_enc_prior)  # x_rec, x_pred_rec, z, z_pred_, Ct
            losses[0].backward()
            optimizer.step()

            losses_agg_tr = agg_losses(losses_agg_tr, losses)

        # log losses
        log_losses(epoch, losses_agg_tr, model.names)

    logging.info("Training is complete")

    # generate datasets:
    args.device = set_seed_device(args.seed)
    model.eval()
    with torch.no_grad():
        generated_data = []
        for data in train_loader:
            n_sample = data[0].shape[0]
            generated_data.append(model.sample_data(n_sample).detach().cpu().numpy())
    generated_data = np.vstack(generated_data)

    logging.info("Data generation is complete")

    ori_data = list()
    for data in train_loader:
        ori_data.append(data[0].detach().cpu().numpy())
    ori_data = np.vstack(ori_data)

    from metrics.discriminative_torch import discriminative_score_metrics
    # deterministic eval
    args.device = set_seed_device(args.seed)
    disc_res = []
    pred_res = []
    for ii in range(10):
        dsc = discriminative_score_metrics(ori_data, generated_data, args)
        disc_res.append(dsc)
        prd = predictive_score_metrics(ori_data, generated_data, args)
        pred_res.append(prd)
    disc_mean, disc_std = np.round(np.mean(disc_res), 4), np.round(np.std(disc_res), 4)
    print('test/disc_mean: ', disc_mean)
    print('test/disc_std: ', disc_std)
    
    pred_mean, pred_std = np.round(np.mean(pred_res), 4), np.round(np.std(pred_res), 4)
    print('test/pred_mean: ', pred_mean)
    print('test/pred_std: ', pred_std)

    visualization (ori_data, generated_data, analysis='tsne', args, run=None)
    visualization (ori_data, generated_data, analysis='pcs', args, run=None)


if __name__ == '__main__':
    main(args)
