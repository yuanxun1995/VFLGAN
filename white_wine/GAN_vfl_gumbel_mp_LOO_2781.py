import argparse
import os
import numpy as np
from numpy.random import noncentral_chisquare
import pandas as pd
from tqdm import tqdm
import multiprocessing as mp

from torch.utils.data import Dataset, DataLoader

from torch.autograd import Variable

import torch.nn as nn
import torch.nn.functional as F
import torch.autograd as autograd
import torch
import random
import matplotlib.pyplot as plt

from scipy.linalg import sqrtm
import warnings 
warnings.filterwarnings('ignore')
torch.cuda.set_device('cuda:'+str(3))


parser = argparse.ArgumentParser()
parser.add_argument("--n_epochs", type=int, default=400, help="number of epochs of training")
parser.add_argument("--batch_size", type=int, default=64, help="size of the batches")
parser.add_argument("--lr", type=float, default=0.0004, help="adam: learning rate")
parser.add_argument("--b1", type=float, default=0.5, help="adam: decay of first order momentum of gradient")
parser.add_argument("--b2", type=float, default=0.999, help="adam: decay of first order momentum of gradient")
parser.add_argument("--n_cpu", type=int, default=8, help="number of cpu threads to use during batch generation")
parser.add_argument("--latent_dim", type=int, default=10, help="dimensionality of the latent space")
parser.add_argument("--img_size", type=int, default=28, help="size of each image dimension")
parser.add_argument("--channels", type=int, default=1, help="number of image channels")
parser.add_argument("--n_critic", type=int, default=5, help="number of training steps for discriminator per iter")
parser.add_argument("--clip_value", type=float, default=0.01, help="lower and upper clip value for disc. weights")
parser.add_argument("--sample_interval", type=int, default=400, help="interval betwen image samples")
opt = parser.parse_args()


cuda = True if torch.cuda.is_available() else False
lambda_gp = 10

Tensor = torch.cuda.FloatTensor if cuda else torch.FloatTensor


def set_random_seed(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    random.seed(seed)


class CustomDataset(Dataset):
    def __init__(self, data, label, delet_target=None, transform=None):
        if delet_target is None:
            self.train_set = data
            self.train_labels = label
        else:
            self.train_set = np.delete(data, delet_target, axis=0)
            self.train_labels = np.delete(label, delet_target)
        self.transform = transform

    def __getitem__(self, index):
        img, target = np.array(self.train_set[index]), int(self.train_labels[index])
        if self.transform is not None:
            img = self.transform(img)
        return img, target

    def __len__(self):
        return len(self.train_set)
    

def calculate_fid(act1, act2):
    # calculate mean and covariance statistics
    mu1, sigma1 = act1.mean(axis=0), np.cov(act1, rowvar=False)
    mu2, sigma2 = act2.mean(axis=0), np.cov(act2, rowvar=False)
    # calculate sum squared difference between means
    ssdiff = np.sum((mu1 - mu2)**2.0)
    # calculate sqrt of product between cov
    covmean = sqrtm(sigma1.dot(sigma2))
    # check and correct imaginary numbers from sqrt
    if np.iscomplexobj(covmean):
        covmean = covmean.real
    # calculate score
    fid = ssdiff + np.trace(sigma1 + sigma2 - 2.0 * covmean)
    return fid


def preprocess(df, delet_target=None):
    data_infor_dict = {}
    if delet_target is not None:
        df.drop(index=delet_target, inplace=True)
    for i in range(11):
        df.iloc[:, i] = (df.iloc[:, i]-df.iloc[:, i].mean()) / df.iloc[:, i].std()
    return df.values


class Generator_1(nn.Module):
    def __init__(self):
        super(Generator_1, self).__init__()

        def block(in_feat, out_feat, normalize=True):
            layers = [nn.Linear(in_feat, out_feat)]
            if normalize:
                layers.append(nn.BatchNorm1d(out_feat, 0.8))
            layers.append(nn.LeakyReLU(0.2, inplace=True))
            return layers

        self.model = nn.Sequential(
            *block(opt.latent_dim, 16, normalize=False),
            *block(16, 32),
            *block(32, 64),
            nn.Linear(64, 6),
        )

    def forward(self, z):
        output = self.model(z)
        return output


class Generator_2(nn.Module):
    def __init__(self, span_info):
        super(Generator_2, self).__init__()

        def block(in_feat, out_feat, normalize=True):
            layers = [nn.Linear(in_feat, out_feat)]
            if normalize:
                layers.append(nn.BatchNorm1d(out_feat, 0.8))
            layers.append(nn.LeakyReLU(0.2, inplace=True))
            return layers

        self.model = nn.Sequential(
            *block(opt.latent_dim, 16, normalize=False),
            *block(16, 32),
            *block(32, 64),
            nn.Linear(64, 5+7),
        )

        self.span_info = span_info

    def _apply_activate(self, data):
        data_t = []
        data_t.append(data[:, :5])
        for span in self.span_info:
            st = span[0]
            ed = span[1]
            transformed = F.gumbel_softmax(data[:, st:ed], tau=0.2, hard=True)
            data_t.append(transformed)
        return torch.cat(data_t, dim=1)

    def forward(self, z):
        output = self.model(z)
        output = self._apply_activate(output)
        return output


class DiscriminatorClient_1(nn.Module):
    def __init__(self):
        super(DiscriminatorClient_1, self).__init__()

        self.model = nn.Sequential(
            nn.Linear(6, 16),
            nn.LeakyReLU(0.2, inplace=True),
            nn.Linear(16, 32),
            nn.LeakyReLU(0.2, inplace=True),
        )

    def forward(self, img):
        img_flat = img.view(img.shape[0], -1)
        latent = self.model(img_flat)
        return latent


class DiscriminatorClient_2(nn.Module):
    def __init__(self):
        super(DiscriminatorClient_2, self).__init__()

        self.model = nn.Sequential(
            nn.Linear(12, 16),
            nn.LeakyReLU(0.2, inplace=True),
            nn.Linear(16, 32),
            nn.LeakyReLU(0.2, inplace=True),
        )

    def forward(self, img):
        img_flat = img.view(img.shape[0], -1)
        latent = self.model(img_flat)
        return latent


class DiscriminatorPrivate(nn.Module):
    def __init__(self):
        super(DiscriminatorPrivate, self).__init__()

        self.model = nn.Sequential(
            nn.Linear(32, 1)
        )

    def forward(self, latent):
        validity = self.model(latent)
        return validity


class DiscriminatorServer(nn.Module):
    def __init__(self):
        super(DiscriminatorServer, self).__init__()

        self.model = nn.Sequential(
            nn.Linear(64, 32),
            nn.LeakyReLU(0.2, inplace=True),
            nn.Linear(32, 1),
        )

    def forward(self, latent):
        validity = self.model(latent)
        return validity


def compute_gradient_penalty_2(D_S, D_C1, D_C2, D_p1, D_p2, real_imgs_client_1, fake_imgs_client_1, real_imgs_client_2, fake_imgs_client_2):
    # Random weight term for interpolation between real and fake samples
    alpha = Tensor(np.random.random((real_imgs_client_1.size(0), 1)))
    # Get random interpolation between real and fake samples
    interpolates_1 = (alpha * real_imgs_client_1 + ((1 - alpha) * fake_imgs_client_1)).requires_grad_(True)
    interpolates_2 = (alpha * real_imgs_client_2 + ((1 - alpha) * fake_imgs_client_2)).requires_grad_(True)

    latent_1 = D_C1(interpolates_1)
    latent_2 = D_C2(interpolates_2)

    d_private_1 = D_p1(latent_1)
    d_private_2 = D_p2(latent_2)

    latent = torch.cat((latent_1, latent_2), dim=1)
    d_interpolates = D_S(latent)

    grad_C = Variable(Tensor(real_imgs_client_1.shape[0], 32).fill_(1.0), requires_grad=False)
    grad_S = Variable(Tensor(real_imgs_client_1.shape[0], 1).fill_(1.0), requires_grad=False)

    gradients_c1 = autograd.grad(
        outputs=latent_1,
        inputs=interpolates_1,
        grad_outputs=grad_C,
        create_graph=True,
        retain_graph=True,
        only_inputs=True,
    )[0]

    gradients_c2 = autograd.grad(
        outputs=latent_2,
        inputs=interpolates_2,
        grad_outputs=grad_C,
        create_graph=True,
        retain_graph=True,
        only_inputs=True,
    )[0]

    gradients_s = autograd.grad(
        outputs=d_interpolates,
        inputs=latent,
        grad_outputs=grad_S,
        create_graph=True,
        retain_graph=True,
        only_inputs=True,
    )[0]

    gradients_cp1 = autograd.grad(
        outputs=d_private_1,
        inputs=latent_1,
        grad_outputs=grad_S,
        create_graph=True,
        retain_graph=True,
        only_inputs=True,
    )[0]

    gradients_cp2 = autograd.grad(
        outputs=d_private_2,
        inputs=latent_2,
        grad_outputs=grad_S,
        create_graph=True,
        retain_graph=True,
        only_inputs=True,
    )[0]

    gradients_c1 = gradients_c1.view(gradients_c1.size(0), -1)
    gradients_c2 = gradients_c2.view(gradients_c2.size(0), -1)
    gradients_cp1 = gradients_cp1.view(gradients_cp1.size(0), -1)
    gradients_cp2 = gradients_cp2.view(gradients_cp2.size(0), -1)
    gradients_s = gradients_s.view(gradients_s.size(0), -1)
    gradient_penalty = ((gradients_c1.norm(2, dim=1) - 1)**2).mean() + ((gradients_c2.norm(2, dim=1) - 1)**2).mean() \
                       + ((gradients_cp1.norm(2, dim=1) - 1)**2).mean() + ((gradients_cp2.norm(2, dim=1)-1)**2).mean() \
                       + ((gradients_s.norm(2, dim=1) - 1) ** 2).mean()
    return gradient_penalty


def train(model_list, optimizer_list, dataloader, data, param_path="params/WGAN_centric_shadow/0/"):
    os.makedirs(param_path, exist_ok=True)

    fid_min = 10
    fid_list = []

    G_1 = model_list[0]
    G_2 = model_list[1]
    D_C1 = model_list[2]
    D_C2 = model_list[3]
    D_p1 = model_list[4]
    D_p2 = model_list[5]
    D_S = model_list[6]

    optimizer_G1 = optimizer_list[0]
    optimizer_G2 = optimizer_list[1]
    optimizer_D1 = optimizer_list[2]
    optimizer_D2 = optimizer_list[3]
    optimizer_Dp1 = optimizer_list[4]
    optimizer_Dp2 = optimizer_list[5]
    optimizer_DS = optimizer_list[6]

    # Training
    batches_done = 0
    for epoch in range(opt.n_epochs):
        for i, (imgs, _) in enumerate(dataloader):
            G_1.train()
            G_2.train()
            D_C1.train()
            D_C2.train()
            D_p1.train()
            D_p2.train()
            D_S.train()
            # Configure input
            imgs_client_1 = imgs[:, :6]
            imgs_client_2 = imgs[:, 6:]
            real_imgs_client_1 = Variable(imgs_client_1.type(Tensor))
            real_imgs_client_2 = Variable(imgs_client_2.type(Tensor))

            optimizer_D1.zero_grad()
            optimizer_D2.zero_grad()
            optimizer_Dp1.zero_grad()
            optimizer_Dp2.zero_grad()
            optimizer_DS.zero_grad()

            # Sample noise as generator input
            z = Variable(Tensor(np.random.normal(0, 1, (imgs.shape[0], opt.latent_dim))))

            # Generate a batch of images
            fake_imgs_client_1 = G_1(z)
            fake_imgs_client_2 = G_2(z)

            # Real images
            latent_1 = D_C1(real_imgs_client_1)
            latent_2 = D_C2(real_imgs_client_2)

            real_private_validity_1 = D_p1(latent_1)
            real_private_validity_2 = D_p2(latent_2)

            latent_real = torch.cat((latent_1, latent_2), dim=1)

            real_validity = D_S(latent_real)
            # Fake images
            latent_1 = D_C1(fake_imgs_client_1)
            latent_2 = D_C2(fake_imgs_client_2)

            latent_fake = torch.cat((latent_1, latent_2), dim=1)

            fake_private_validity_1 = D_p1(latent_1)
            fake_private_validity_2 = D_p2(latent_2)

            fake_validity = D_S(latent_fake)

            gradient_penalty_DS = compute_gradient_penalty_2(D_S, D_C1, D_C2, D_p1, D_p2, real_imgs_client_1.data,
                                                             fake_imgs_client_1.data, real_imgs_client_2.data,
                                                             fake_imgs_client_2.data)

            # Adversarial loss
            lambda_dp = 1.0
            d_loss = -torch.mean(real_validity) - lambda_dp*torch.mean(real_private_validity_1) - lambda_dp*torch.mean(real_private_validity_2) + \
                      torch.mean(fake_validity) + lambda_dp*torch.mean(fake_private_validity_1) + lambda_dp*torch.mean(fake_private_validity_2) + \
                      lambda_gp * gradient_penalty_DS

            d_loss.backward()
            optimizer_D1.step()
            optimizer_D2.step()
            optimizer_Dp1.step()
            optimizer_Dp2.step()
            optimizer_DS.step()

            optimizer_G1.zero_grad()
            optimizer_G2.zero_grad()

            # Train the generator every n_critic steps
            if i % opt.n_critic == 0:
                # Generate a batch of images
                fake_imgs_client_1 = G_1(z)
                fake_imgs_client_2 = G_2(z)

                # Train on fake images
                latent_1 = D_C1(fake_imgs_client_1)
                latent_2 = D_C2(fake_imgs_client_2)
                latent_fake = torch.cat((latent_1, latent_2), dim=1)

                fake_private_validity_1 = D_p1(latent_1)
                fake_private_validity_2 = D_p2(latent_2)

                fake_validity = D_S(latent_fake)

                g_loss = -torch.mean(fake_validity) - lambda_dp * (torch.mean(fake_private_validity_1) + torch.mean(fake_private_validity_2))
                g_loss.backward()

                optimizer_G1.step()
                optimizer_G2.step()

        G_1.eval()
        G_2.eval()

        z = Variable(Tensor(np.random.normal(0, 1, (data.shape[0], opt.latent_dim))))

        fake_imgs_client_1 = G_1(z)
        fake_imgs_client_2 = G_2(z)
        fake_data = torch.cat((fake_imgs_client_1, fake_imgs_client_2), dim=1)

        fake_data = fake_data.cpu().detach().numpy()

        fid = calculate_fid(data, fake_data)
        fid_list += [fid]

        if fid < fid_min:
            torch.save(G_1.state_dict(), param_path + "G_1.pth")
            torch.save(G_2.state_dict(), param_path + "G_2.pth")
            torch.save(D_C1.state_dict(), param_path + "D_C1.pth")
            torch.save(D_C2.state_dict(), param_path + "D_C2.pth")
            fid_min = fid
            min_epoch = epoch

    title = 'min_fid_' + str(min(fid_list)) + '_epoch_' + str(min_epoch) + '.npy'
    fid_np = np.array(fid_list)
    np.save(param_path+title, fid_np)

    t = [i for i in range(len(fid_list))]
    plt.plot(t, fid_list)
    plt.title(title)
    plt.xlabel('epoch')
    plt.ylabel('fid score')
    plt.savefig(param_path + 'fid.jpg')

#
# if __name__ == '__main__':
#     fid_min = []
#     for seed in tqdm(range(1)):
#         param_path = "params/WGAN_vfl_gumbel_shadow/" + str(seed) + '/'
#         fid_min += [train(seed=seed, param_path=param_path)]
#
#     f = open("params/WGAN_vfl_gumbel_shadow/fid_min.txt", "w")
#     f.write(str(fid_min))
#     f.close()


def initialization(seed, delet_target=None):
    set_random_seed(seed)

    df = pd.read_csv('data/winequality-white-onehot.csv')
    data = preprocess(df, delet_target=delet_target)
    labels = np.zeros(data.shape[0])
    dataset = CustomDataset(data, labels)
    dataloader = DataLoader(dataset, batch_size=opt.batch_size, shuffle=True)

    # Initialize generator and discriminator
    G_1 = Generator_1()
    span_info = [(5, 12)]
    G_2 = Generator_2(span_info)

    D_C1 = DiscriminatorClient_1()
    D_C2 = DiscriminatorClient_2()

    D_p1 = DiscriminatorPrivate()
    D_p2 = DiscriminatorPrivate()

    D_S = DiscriminatorServer()

    if cuda:
        G_1.cuda()
        G_2.cuda()
        D_C1.cuda()
        D_C2.cuda()
        D_p1.cuda()
        D_p2.cuda()
        D_S.cuda()

    # Optimizers
    optimizer_G1 = torch.optim.Adam(G_1.parameters(), lr=opt.lr, betas=(opt.b1, opt.b2))
    optimizer_G2 = torch.optim.Adam(G_2.parameters(), lr=opt.lr, betas=(opt.b1, opt.b2))
    optimizer_D1 = torch.optim.Adam(D_C1.parameters(), lr=opt.lr, betas=(opt.b1, opt.b2))
    optimizer_D2 = torch.optim.Adam(D_C2.parameters(), lr=opt.lr, betas=(opt.b1, opt.b2))
    optimizer_Dp1 = torch.optim.Adam(D_p1.parameters(), lr=opt.lr, betas=(opt.b1, opt.b2))
    optimizer_Dp2 = torch.optim.Adam(D_p2.parameters(), lr=opt.lr, betas=(opt.b1, opt.b2))
    optimizer_DS = torch.optim.Adam(D_S.parameters(), lr=opt.lr, betas=(opt.b1, opt.b2))

    model_list = [G_1, G_2, D_C1, D_C2, D_p1, D_p2, D_S]
    optimizer_list = [optimizer_G1, optimizer_G2, optimizer_D1, optimizer_D2, optimizer_Dp1, optimizer_Dp2, optimizer_DS]

    return model_list, optimizer_list, dataloader, data


if __name__ == '__main__':
    target = 2781 

    mp = mp.get_context('spawn')

    for i in tqdm(range(25)):
        for j in range(4):
            seed = 4 * i + j
            model_list, optimizer_list, dataloader, data_new = initialization(seed, delet_target=target)
            if target is not None:
                param_path = 'params/WGAN_vfl_LOO_non_epsilon_' + str(target) + '/' + str(seed) + '/'
            else:
                param_path = 'params/WGAN_vfl_shadow_non_epsilon' + '/' + str(seed) + '/'
            if j == 0:
                p0 = mp.Process(target=train, args=(model_list, optimizer_list, dataloader, data_new, param_path))
            elif j == 1:
                p1 = mp.Process(target=train, args=(model_list, optimizer_list, dataloader, data_new, param_path))
            elif j == 2:
                p2 = mp.Process(target=train, args=(model_list, optimizer_list, dataloader, data_new, param_path))
            elif j == 3:
                p3 = mp.Process(target=train, args=(model_list, optimizer_list, dataloader, data_new, param_path))

        p0.start()
        p1.start()
        p2.start()
        p3.start()
        p0.join()
        p1.join()
        p2.join()
        p3.join()