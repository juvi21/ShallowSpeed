import time
from pathlib import Path

import numpy as np
import pandas as pd
from mpi4py import MPI
from sklearn.datasets import fetch_openml
from sklearn.model_selection import train_test_split

from minMLP.functional import mse_loss, mse_loss_grad
from minMLP.models import MLP, Distributed_MLP
from minMLP.optimizer import SGD
from minMLP.utils import rprint, get_model_hash, assert_sync


def compute_accuracy(model, x_val, y_val):
    """
    This function does a forward pass of x, then checks if the indices
    of the maximum value in the output equals the indices in the label
    y. Then it sums over each prediction and calculates the accuracy.
    """
    x_val = x_val.to_numpy()

    model.eval()
    output = model.forward(x_val)
    model.train()

    pred = np.argmax(output, axis=-1)
    target = np.argmax(y_val, axis=-1)
    return np.mean(pred == target)


def download_dataset(save_dir):
    x, y = fetch_openml("mnist_784", version=1, data_home="data_cache", return_X_y=True)

    x /= 255.0
    x -= x.mean()
    y = pd.get_dummies(y)

    x_train, x_val, y_train, y_val = train_test_split(
        x, y, test_size=0.15, random_state=42
    )
    save_dir.mkdir()
    x_train.to_parquet(save_dir / "x_train.parquet")
    x_val.to_parquet(save_dir / "x_val.parquet")
    np.save(save_dir / "y_train.npy", y_train)
    np.save(save_dir / "y_val.npy", y_val)


EPOCHS = 10
# We use a big batch size, to make training more amenable to parallelization
GLOBAL_BATCH_SIZE = 128

if __name__ == "__main__":
    # init MPI
    comm = MPI.COMM_WORLD
    rank = comm.Get_rank()
    size = comm.Get_size()

    save_dir = Path("../data/mnist_784/")
    if not save_dir.is_dir():
        if rank == 0:
            print("Downloading the dataset at", save_dir.resolve())
            download_dataset(save_dir)
        # make all processes wait until dataset is downloaded
        comm.Barrier()

    # each process loads the whole dataset
    # this is inefficient for large datasets, but fine for tiny MNIST
    x_train = pd.read_parquet(save_dir / "x_train.parquet").to_numpy()
    y_train = np.load(save_dir / "y_train.npy")
    x_val = pd.read_parquet(save_dir / "x_val.parquet")
    y_val = np.load(save_dir / "y_val.npy")

    # each process selects its subset of the datasets by a `rank`-offset and `size`-strides
    # the copy() is super important, else the array is not continuous in memory
    # which results in horrible matmul performance
    x_train = x_train[rank : len(x_train) : size].copy()
    y_train = y_train[rank : len(y_train) : size].copy()
    assert GLOBAL_BATCH_SIZE % size == 0
    batch_size = GLOBAL_BATCH_SIZE // size

    layer_sizes = [784, 128, 10]
    if size == 1:
        model = MLP(sizes=layer_sizes)
    else:
        model = Distributed_MLP(sizes=layer_sizes, comm=comm)
    # batch size is huge, so we can use a big learning rate
    optimizer = SGD(model.parameters(), lr=0.1)

    start_time = time.time()
    model.train()
    for iteration in range(EPOCHS):
        accuracy = compute_accuracy(model, x_val, y_val)
        rprint(
            "Epoch: {0}, Time Spent: {1:.2f}s, Accuracy: {2:.2f}%".format(
                iteration, time.time() - start_time, accuracy * 100
            ),
        )
        for j in range(0, len(x_train), batch_size):
            x = x_train[j : min(len(x_train), j + batch_size)]
            y = y_train[j : min(len(y_train), j + batch_size)]

            output = model.forward(x)
            loss = mse_loss(output, y)
            dout = mse_loss_grad(output, y)

            model.zero_grad()
            model.backward(dout)
            optimizer.step()

    accuracy = compute_accuracy(model, x_val, y_val)
    rprint(
        "Epoch: {0}, Time Spent: {1:.2f}s, Accuracy: {2:.2f}%".format(
            EPOCHS, time.time() - start_time, accuracy * 100
        ),
    )

    # Sanity check: Make sure processes have the same model weights
    assert_sync(comm, get_model_hash(model))
