import importlib
import os
import numpy as np
import cv2
import torch
import torch.distributed as dist
import torchvision
import sys

def count_params(model, verbose=False):
    total_params = sum(p.numel() for p in model.parameters())
    if verbose:
        print(f"{model.__class__.__name__} has {total_params*1.e-6:.2f} M params.")
    return total_params


def check_istarget(name, para_list):
    """
    name: full name of source para
    para_list: partial name of target para
    """
    istarget = False
    for para in para_list:
        if para in name:
            return True
    return istarget


def instantiate_from_config(config):
    if not "target" in config:
        if config == "__is_first_stage__":
            return None
        elif config == "__is_unconditional__":
            return None
        raise KeyError("Expected key `target` to instantiate.")
    return get_obj_from_str(config["target"])(**config.get("params", dict()))

def get_obj_from_str(string, reload=False):
    # Get the current directory
    current_dir = os.path.abspath(os.path.dirname(__file__))
    
    # Move up to the `t2v_turbo` directory
    while os.path.basename(current_dir) not in ['t2v_turbo', 'videogen_hub']:
        current_dir = os.path.dirname(current_dir)
        if current_dir == os.path.dirname(current_dir):  # Reached the root directory
            raise FileNotFoundError("Couldn't find 't2v_turbo' or 'videogen_hub' in the path hierarchy")
    
    # Construct the paths for `pipelines` and `t2v_turbo`
    paths_to_add = []
    if os.path.basename(current_dir) == 't2v_turbo':
        paths_to_add.append(current_dir)
        paths_to_add.append(os.path.join(current_dir, '..'))  # Up one level to the 'pipelines' directory
    elif os.path.basename(current_dir) == 'videogen_hub':
        paths_to_add.append(os.path.join(current_dir, 'pipelines'))
        paths_to_add.append(os.path.join(current_dir, 'pipelines', 't2v_turbo'))

    # Normalize paths to avoid issues with '..'
    paths_to_add = [os.path.normpath(path) for path in paths_to_add]

    print("+++++> string", string)
    print("+++++> base_dir", current_dir)
    print("+++++> paths_to_add", paths_to_add)

    # Add the paths to sys.path if they're not already there
    for path in paths_to_add:
        if path not in sys.path:
            sys.path.insert(0, path)
    
    # Extract the module and class names
    module, cls = string.rsplit(".", 1)

    # Import and optionally reload the module
    module_imp = importlib.import_module(module)
    if reload:
        importlib.reload(module_imp)
    
    # Get the class from the module
    return getattr(module_imp, cls)

"""
def get_obj_from_str(string, reload=False):
    module, cls = string.rsplit(".", 1)
    if reload:
        module_imp = importlib.import_module(module)
        importlib.reload(module_imp)
    return getattr(importlib.import_module(module, package=None), cls)
"""

def load_npz_from_dir(data_dir):
    data = [
        np.load(os.path.join(data_dir, data_name))["arr_0"]
        for data_name in os.listdir(data_dir)
    ]
    data = np.concatenate(data, axis=0)
    return data


def load_npz_from_paths(data_paths):
    data = [np.load(data_path)["arr_0"] for data_path in data_paths]
    data = np.concatenate(data, axis=0)
    return data


def resize_numpy_image(image, max_resolution=512 * 512, resize_short_edge=None):
    h, w = image.shape[:2]
    if resize_short_edge is not None:
        k = resize_short_edge / min(h, w)
    else:
        k = max_resolution / (h * w)
        k = k**0.5
    h = int(np.round(h * k / 64)) * 64
    w = int(np.round(w * k / 64)) * 64
    image = cv2.resize(image, (w, h), interpolation=cv2.INTER_LANCZOS4)
    return image


def setup_dist(args):
    if dist.is_initialized():
        return
    torch.cuda.set_device(args.local_rank)
    torch.distributed.init_process_group("nccl", init_method="env://")


def save_videos(batch_tensors, savedir, filenames, fps=16):
    # b,samples,c,t,h,w
    n_samples = batch_tensors.shape[1]
    for idx, vid_tensor in enumerate(batch_tensors):
        video = vid_tensor.detach().cpu()
        video = torch.clamp(video.float(), -1.0, 1.0)
        video = video.permute(2, 0, 1, 3, 4)  # t,n,c,h,w
        frame_grids = [
            torchvision.utils.make_grid(framesheet, nrow=int(n_samples))
            for framesheet in video
        ]  # [3, 1*h, n*w]
        grid = torch.stack(frame_grids, dim=0)  # stack in temporal dim [t, 3, n*h, w]
        grid = (grid + 1.0) / 2.0
        grid = (grid * 255).to(torch.uint8).permute(0, 2, 3, 1)
        savepath = os.path.join(savedir, f"{filenames[idx]}.mp4")
        torchvision.io.write_video(
            savepath, grid, fps=fps, video_codec="h264", options={"crf": "10"}
        )