import math
import random
from copy import deepcopy
from re import L
from typing import List, Optional, Union

import cv2
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision.transforms.functional as tvF
from pytorch3d.transforms import (
    axis_angle_to_matrix,
    axis_angle_to_quaternion,
    euler_angles_to_matrix,
    matrix_to_euler_angles,
    matrix_to_quaternion,
    matrix_to_rotation_6d,
    quaternion_to_axis_angle,
    quaternion_to_matrix,
    rotation_6d_to_matrix,
)
from scipy.spatial.transform import Rotation as R
from scipy.spatial.transform import Slerp


class RandomOcclusion:
    """Add random occlusion based on occlusion probability.

    Args:
        occlusion_prob (float): probability of the image having
        occlusion. Default: 0.5
    """

    def __init__(self, occlusion_prob=0.5):
        self.occlusion_prob = occlusion_prob

    def __call__(self, results):
        if np.random.rand() > self.occlusion_prob:
            return results

        xmin, ymin, xmax, ymax = results["bbox"]
        imgwidth = results["width"]
        imgheight = results["height"]
        img = results["image"]

        area_min = 0.0
        area_max = 0.2
        synth_area = (random.random() * (area_max - area_min) + area_min) * (xmax - xmin) * (ymax - ymin)

        ratio_min = 0.5
        ratio_max = 1 / 0.5
        synth_ratio = random.random() * (ratio_max - ratio_min) + ratio_min

        synth_h = math.sqrt(synth_area * synth_ratio)
        synth_w = math.sqrt(synth_area / synth_ratio)
        synth_xmin = random.random() * ((xmax - xmin) - synth_w - 1) + xmin
        synth_ymin = random.random() * ((ymax - ymin) - synth_h - 1) + ymin

        if synth_xmin >= 0 and synth_ymin >= 0 and synth_xmin + synth_w < imgwidth and synth_ymin + synth_h < imgheight:
            synth_xmin = int(synth_xmin)
            synth_ymin = int(synth_ymin)
            synth_w = int(synth_w)
            synth_h = int(synth_h)
            img[synth_ymin : synth_ymin + synth_h, synth_xmin : synth_xmin + synth_w, :] = (
                np.random.rand(synth_h, synth_w, 3) * 255
            )

        results["image"] = img
        return results


class Compose:
    def __init__(self, transforms: list):
        """Composes several transforms together. This transform does not
        support torchscript.

        Args:
            transforms (list): (list of transform functions)
        """
        self.transforms = transforms

    def __call__(self, rotation: Union[torch.Tensor, np.ndarray], convention: str = "xyz", **kwargs):
        convention = convention.lower()
        if not (set(convention) == set("xyz") and len(convention) == 3):
            raise ValueError(f"Invalid convention {convention}.")
        if isinstance(rotation, np.ndarray):
            data_type = "numpy"
            rotation = torch.FloatTensor(rotation)
        elif isinstance(rotation, torch.Tensor):
            data_type = "tensor"
        else:
            raise TypeError("Type of rotation should be torch.Tensor or numpy.ndarray")
        for t in self.transforms:
            if "convention" in t.__code__.co_varnames:
                rotation = t(rotation, convention.upper(), **kwargs)
            else:
                rotation = t(rotation, **kwargs)
        if data_type == "numpy":
            rotation = rotation.detach().cpu().numpy()
        return rotation


def aa_to_rotmat(axis_angle: Union[torch.Tensor, np.ndarray]) -> Union[torch.Tensor, np.ndarray]:
    """
    Convert axis_angle to rotation matrixs.
    Args:
        axis_angle (Union[torch.Tensor, numpy.ndarray]): input shape
                should be (..., 3). ndim of input is unlimited.

    Returns:
        Union[torch.Tensor, numpy.ndarray]: shape would be (..., 3, 3).
    """
    if axis_angle.shape[-1] != 3:
        raise ValueError(f"Invalid input axis angles shape f{axis_angle.shape}.")
    t = Compose([axis_angle_to_matrix])
    return t(axis_angle)


def rotmat_to_aa(matrix: Union[torch.Tensor, np.ndarray]) -> Union[torch.Tensor, np.ndarray]:
    """Convert rotation matrixs to axis angles.

    Args:
        matrix (Union[torch.Tensor, numpy.ndarray]): input shape
                should be (..., 3, 3). ndim of input is unlimited.
        convention (str, optional): Convention string of three letters
                from {“x”, “y”, and “z”}. Defaults to 'xyz'.

    Returns:
        Union[torch.Tensor, numpy.ndarray]: shape would be (..., 3).
    """
    if matrix.shape[-1] != 3 or matrix.shape[-2] != 3:
        raise ValueError(f"Invalid rotation matrix  shape f{matrix.shape}.")
    t = Compose([matrix_to_quaternion, quaternion_to_axis_angle])
    return t(matrix)


def aa_to_quat(axis_angle: Union[torch.Tensor, np.ndarray]) -> Union[torch.Tensor, np.ndarray]:
    """
    Convert axis_angle to quaternions.
    Args:
        axis_angle (Union[torch.Tensor, numpy.ndarray]): input shape
                should be (..., 3). ndim of input is unlimited.

    Returns:
        Union[torch.Tensor, numpy.ndarray]: shape would be (..., 4).
    """
    if axis_angle.shape[-1] != 3:
        raise ValueError(f"Invalid input axis angles f{axis_angle.shape}.")
    t = Compose([axis_angle_to_quaternion])
    return t(axis_angle)


def aa_to_rot6d(axis_angle: Union[torch.Tensor, np.ndarray]) -> Union[torch.Tensor, np.ndarray]:
    """Convert axis angles to rotation 6d representations.

    Args:
        axis_angle (Union[torch.Tensor, numpy.ndarray]): input shape
                should be (..., 3). ndim of input is unlimited.

    Returns:
        Union[torch.Tensor, numpy.ndarray]: shape would be (..., 6).

    [1] Zhou, Y., Barnes, C., Lu, J., Yang, J., & Li, H.
    On the Continuity of Rotation Representations in Neural Networks.
    IEEE Conference on Computer Vision and Pattern Recognition, 2019.
    Retrieved from http://arxiv.org/abs/1812.07035
    """
    if axis_angle.shape[-1] != 3:
        raise ValueError(f"Invalid input axis_angle f{axis_angle.shape}.")
    t = Compose([axis_angle_to_matrix, matrix_to_rotation_6d])
    return t(axis_angle)


def rot6d_to_aa(rotation_6d: Union[torch.Tensor, np.ndarray]) -> Union[torch.Tensor, np.ndarray]:
    """Convert rotation 6d representations to axis angles.

    Args:
        rotation_6d (Union[torch.Tensor, numpy.ndarray]): input shape
                should be (..., 6). ndim of input is unlimited.

    Returns:
        Union[torch.Tensor, numpy.ndarray]: shape would be (..., 3).

    [1] Zhou, Y., Barnes, C., Lu, J., Yang, J., & Li, H.
    On the Continuity of Rotation Representations in Neural Networks.
    IEEE Conference on Computer Vision and Pattern Recognition, 2019.
    Retrieved from http://arxiv.org/abs/1812.07035
    """
    if rotation_6d.shape[-1] != 6:
        raise ValueError(f"Invalid input rotation_6d f{rotation_6d.shape}.")
    t = Compose([rotation_6d_to_matrix, matrix_to_quaternion, quaternion_to_axis_angle])
    return t(rotation_6d)


def quat_to_aa(quaternions: Union[torch.Tensor, np.ndarray]) -> Union[torch.Tensor, np.ndarray]:
    """Convert quaternions to axis angles.

    Args:
        quaternions (Union[torch.Tensor, numpy.ndarray]): input shape
                should be (..., 3). ndim of input is unlimited.
    Returns:
        Union[torch.Tensor, numpy.ndarray]: shape would be (..., 3).
    """
    if quaternions.shape[-1] != 4:
        raise ValueError(f"Invalid input quaternions f{quaternions.shape}.")
    t = Compose([quaternion_to_axis_angle])
    return t(quaternions)


def rot6d_to_rotmat(rotation_6d: Union[torch.Tensor, np.ndarray]) -> Union[torch.Tensor, np.ndarray]:
    """Convert rotation 6d representations to rotation matrixs.

    Args:
        rotation_6d (Union[torch.Tensor, numpy.ndarray]): input shape
                should be (..., 6). ndim of input is unlimited.
    Returns:
        Union[torch.Tensor, numpy.ndarray]: shape would be (..., 3, 3).

    [1] Zhou, Y., Barnes, C., Lu, J., Yang, J., & Li, H.
    On the Continuity of Rotation Representations in Neural Networks.
    IEEE Conference on Computer Vision and Pattern Recognition, 2019.
    Retrieved from http://arxiv.org/abs/1812.07035
    """
    if rotation_6d.shape[-1] != 6:
        raise ValueError(f"Invalid input rotation_6d f{rotation_6d.shape}.")
    t = Compose([rotation_6d_to_matrix])
    return t(rotation_6d)


def rotmat_to_rot6d(matrix: Union[torch.Tensor, np.ndarray]) -> Union[torch.Tensor, np.ndarray]:
    """Convert rotation matrixs to rotation 6d representations.

    Args:
        matrix (Union[torch.Tensor, numpy.ndarray]): input shape
                should be (..., 3, 3). ndim of input is unlimited.
    Returns:
        Union[torch.Tensor, numpy.ndarray]: shape would be (..., 6).

    [1] Zhou, Y., Barnes, C., Lu, J., Yang, J., & Li, H.
    On the Continuity of Rotation Representations in Neural Networks.
    IEEE Conference on Computer Vision and Pattern Recognition, 2019.
    Retrieved from http://arxiv.org/abs/1812.07035
    """
    if matrix.shape[-1] != 3 or matrix.shape[-2] != 3:
        raise ValueError(f"Invalid rotation matrix  shape f{matrix.shape}.")
    t = Compose([matrix_to_rotation_6d])
    return t(matrix)


def rotmat_to_quat(matrix: Union[torch.Tensor, np.ndarray]) -> Union[torch.Tensor, np.ndarray]:
    """Convert rotation matrixs to quaternions.

    Args:
        matrix (Union[torch.Tensor, numpy.ndarray]): input shape
                should be (..., 3, 3). ndim of input is unlimited.
    Returns:
        Union[torch.Tensor, numpy.ndarray]: shape would be (..., 4).
    """
    if matrix.shape[-1] != 3 or matrix.shape[-2] != 3:
        raise ValueError(f"Invalid rotation matrix  shape f{matrix.shape}.")
    t = Compose([matrix_to_quaternion])
    return t(matrix)


def quat_to_rotmat(quaternions: Union[torch.Tensor, np.ndarray]) -> Union[torch.Tensor, np.ndarray]:
    """Convert quaternions to rotation matrixs.

    Args:
        quaternions (Union[torch.Tensor, numpy.ndarray]): input shape
                should be (..., 3). ndim of input is unlimited.
    Returns:
        Union[torch.Tensor, numpy.ndarray]: shape would be (..., 3, 3).
    """
    if quaternions.shape[-1] != 4:
        raise ValueError(f"Invalid input quaternions shape f{quaternions.shape}.")
    t = Compose([quaternion_to_matrix])
    return t(quaternions)


def quat_to_rot6d(quaternions: Union[torch.Tensor, np.ndarray]) -> Union[torch.Tensor, np.ndarray]:
    """Convert quaternions to rotation 6d representations.

    Args:
        quaternions (Union[torch.Tensor, numpy.ndarray]): input shape
                should be (..., 4). ndim of input is unlimited.

    Returns:
        Union[torch.Tensor, numpy.ndarray]: shape would be (..., 6).

    [1] Zhou, Y., Barnes, C., Lu, J., Yang, J., & Li, H.
    On the Continuity of Rotation Representations in Neural Networks.
    IEEE Conference on Computer Vision and Pattern Recognition, 2019.
    Retrieved from http://arxiv.org/abs/1812.07035
    """
    if quaternions.shape[-1] != 4:
        raise ValueError(f"Invalid input quaternions f{quaternions.shape}.")
    t = Compose([quaternion_to_matrix, matrix_to_rotation_6d])
    return t(quaternions)


def rot6d_to_quat(rotation_6d: Union[torch.Tensor, np.ndarray]) -> Union[torch.Tensor, np.ndarray]:
    """Convert rotation 6d representations to quaternions.

    Args:
        rotation (Union[torch.Tensor, numpy.ndarray]): input shape
                should be (..., 6). ndim of input is unlimited.

    Returns:
        Union[torch.Tensor, numpy.ndarray]: shape would be (..., 4).

    [1] Zhou, Y., Barnes, C., Lu, J., Yang, J., & Li, H.
    On the Continuity of Rotation Representations in Neural Networks.
    IEEE Conference on Computer Vision and Pattern Recognition, 2019.
    Retrieved from http://arxiv.org/abs/1812.07035
    """
    if rotation_6d.shape[-1] != 6:
        raise ValueError(f"Invalid input rotation_6d shape f{rotation_6d.shape}.")
    t = Compose([rotation_6d_to_matrix, matrix_to_quaternion])
    return t(rotation_6d)


def _rotate_smpl_pose(pose, rot):
    """Rotate SMPL pose parameters.

    SMPL (https://smpl.is.tue.mpg.de/) is a 3D human model.
    Args:
        pose (np.ndarray([72])): SMPL pose parameters
        rot (float): Rotation rad.
    Returns:
        pose_rotated
    """
    rot_mat = _construct_rotation_matrix(rot)
    pose_rotated = pose.copy()
    orient = pose[:3]
    orient_mat = aa_to_rotmat(orient)

    new_orient_mat = np.matmul(rot_mat, orient_mat)
    new_orient = rotmat_to_aa(new_orient_mat)
    pose_rotated[:3] = new_orient

    return pose_rotated


def _construct_rotation_matrix(rot, size=3):
    """Construct the in-plane rotation matrix.

    Args:
        rot (float): Rotation rad.
        size (int): The size of the rotation matrix.
            Candidate Values: 2, 3. Defaults to 3.
    Returns:
        rot_mat (np.ndarray([size, size]): Rotation matrix.
    """
    rot_mat = np.eye(size, dtype=np.float32)
    if rot != 0:
        sn, cs = np.sin(rot), np.cos(rot)
        rot_mat[0, :2] = [cs, -sn]
        rot_mat[1, :2] = [sn, cs]

    return rot_mat


def _transform_coords(pts, affine_trans, invert=False):
    """
    Args:
        pts(np.ndarray): (point_nb, 2)
    """
    if invert:
        affine_trans = np.linalg.inv(affine_trans)
    hom2d = np.concatenate([pts, np.ones([np.array(pts).shape[0], 1])], 1)
    transformed_rows = affine_trans.dot(hom2d.transpose()).transpose()[:, :2]
    return transformed_rows


def _get_affine_transform(center, scale, optical_center, out_res, rot=0):
    rot_mat = np.zeros((3, 3))
    sn, cs = np.sin(rot), np.cos(rot)
    rot_mat[0, :2] = [cs, -sn]
    rot_mat[1, :2] = [sn, cs]
    rot_mat[2, 2] = 1
    # Rotate center to obtain coordinate of center in rotated image
    origin_rot_center = rot_mat.dot(center.tolist() + [1])[:2]
    # Get center for transform with verts rotated around optical axis
    # (through pixel center, smthg like 128, 128 in pixels and 0,0 in 3d world)
    # For this, rotate the center but around center of image (vs 0,0 in pixel space)
    t_mat = np.eye(3)
    t_mat[0, 2] = -optical_center[0]
    t_mat[1, 2] = -optical_center[1]
    t_inv = t_mat.copy()
    t_inv[:2, 2] *= -1
    transformed_center = t_inv.dot(rot_mat).dot(t_mat).dot(center.tolist() + [1])
    post_rot_trans = _get_affine_trans_no_rot(origin_rot_center, scale, out_res)
    total_trans = post_rot_trans.dot(rot_mat)
    # check_t = get_affine_transform_bak(center, scale, res, rot)
    # print(total_trans, check_t)
    affinetrans_post_rot = _get_affine_trans_no_rot(transformed_center[:2], scale, out_res)
    return total_trans.astype(np.float32), affinetrans_post_rot.astype(np.float32)


def _affine_transform(center, scale, out_res, rot=0):
    rotmat = _construct_rotation_matrix(rot=rot, size=3)
    # Rotate center to obtain coordinate of center in rotated image
    origin_rot_center = (rotmat.dot(np.concatenate([center, np.ones(1)])))[:2]

    post_rot_trans = _get_affine_trans_no_rot(origin_rot_center, scale, out_res)
    total_trans = post_rot_trans.dot(rotmat)
    return total_trans.astype(np.float32)


def _affine_transform_post_rot(center, scale, optical_center, out_res, rot=0):
    rotmat = _construct_rotation_matrix(rot=rot, size=3)
    t_mat = np.eye(3)
    t_mat[0, 2] = -optical_center[0]
    t_mat[1, 2] = -optical_center[1]
    t_inv = t_mat.copy()
    t_inv[:2, 2] *= -1
    transformed_center = t_inv.dot(rotmat).dot(t_mat).dot(np.concatenate([center, np.ones(1)]))
    affine_trans_post_rot = _get_affine_trans_no_rot(transformed_center[:2], scale, out_res)

    return affine_trans_post_rot.astype(np.float32)


def _get_affine_trans_no_rot(center, scale, res):
    affinet = np.zeros((3, 3))
    scale_ratio = float(res[0]) / float(res[1])
    affinet[0, 0] = float(res[0]) / scale
    affinet[1, 1] = float(res[1]) / scale * scale_ratio
    affinet[0, 2] = res[0] * (-float(center[0]) / scale + 0.5)
    affinet[1, 2] = res[1] * (-float(center[1]) / scale * scale_ratio + 0.5)
    affinet[2, 2] = 1
    return affinet


def fit_ortho_param(joints3d: np.ndarray, joints2d: np.ndarray) -> np.ndarray:
    joints3d_xy = joints3d[:, :2]  # (21, 2)
    joints3d_xy = joints3d_xy.reshape(-1)[:, np.newaxis]
    joints2d = joints2d.reshape(-1)[:, np.newaxis]
    pad2 = np.array(range(joints2d.shape[0]))
    pad2 = (pad2 % 2)[:, np.newaxis]
    pad1 = 1 - pad2
    jM = np.concatenate([joints3d_xy, pad1, pad2], axis=1)  # (42, 3)
    jMT = jM.transpose()  # (3, 42)
    jMTjM = np.matmul(jMT, jM)
    jMTb = np.matmul(jMT, joints2d)
    ortho_param = np.matmul(np.linalg.inv(jMTjM), jMTb)
    ortho_param = ortho_param.reshape(-1)
    return ortho_param  # [f, tx, ty]


def batch_xyz2uvd(
    xyz: torch.Tensor,
    root_joint: torch.Tensor,
    intr: torch.Tensor,
    inp_res: List[int],
    depth_range=0.4,
    ref_bone_len: Optional[torch.Tensor] = None,
    camera_mode="persp",
) -> torch.Tensor:
    inp_res = torch.Tensor(inp_res).to(xyz.device)  # TENSOR (2,)
    batch_size = xyz.shape[0]
    if ref_bone_len is None:
        ref_bone_len = torch.ones((batch_size, 1)).to(xyz.device)  # TENSOR (B, 1)

    if camera_mode == "persp":
        assert intr.dim() == 3, f"Unexpected dim, expect intr has shape (B, 3, 3), got {intr.shape}"
        #  1. normalize depth : root_relative, scale_invariant
        z = xyz[:, :, 2]  # TENSOR (B, NKP)
        xy = xyz[:, :, :2]  # TENSOR (B, NKP, 2)
        xy_ = xy / z.unsqueeze(-1).expand_as(xy)  # TENSOR (B, NKP, 2)
        root_joint_z = root_joint[:, -1].unsqueeze(-1)  # TENSOR (B, 1)
        z_ = (z - root_joint_z.expand_as(z)) / ref_bone_len.expand_as(z)  # TENSOR (B, NKP)

        #  2. xy_ -> uv
        fx = intr[:, 0, 0].unsqueeze(-1)  # TENSOR (B, 1)
        fy = intr[:, 1, 1].unsqueeze(-1)
        cx = intr[:, 0, 2].unsqueeze(-1)
        cy = intr[:, 1, 2].unsqueeze(-1)
        # cat 4 TENSOR (B, 1)
        camparam = torch.cat((fx, fy, cx, cy), dim=1)  # TENSOR (B, 4)
        camparam = camparam.unsqueeze(1).expand(-1, xyz.shape[1], -1)  # TENSOR (B, NKP, 4)
        uv = (xy_ * camparam[:, :, :2]) + camparam[:, :, 2:4]  # TENSOR (B, NKP, 2)

        #  3. normalize uvd to 0~1
        uv = torch.einsum("bij, j->bij", uv, 1.0 / inp_res)  # TENSOR (B, NKP, 2), [0 ~ 1]
        d = z_ / depth_range + 0.5  # TENSOR (B, NKP), [0 ~ 1]
        uvd = torch.cat((uv, d.unsqueeze(-1)), -1)  # TENSOR (B, NKP, 3)
    elif camera_mode == "ortho":
        assert intr.dim() == 2, f"Unexpected dim, expect intr has shape (B, 3), got {intr.shape}"
        # root_relative
        xyz = xyz - root_joint.unsqueeze(1)  # TENSOR (B, NKP, 3)

        xy = xyz[:, :, :2]  # TENSOR (B, NKP, 2)
        z = xyz[:, :, 2]  # TENSOR (B, NKP)
        z_ = z / ref_bone_len.expand_as(z)  # TENSOR (B, NKP)
        d = z_ / depth_range + 0.5  # TENSOR (B, NKP), [0 ~ 1]

        scale = intr[:, :1].unsqueeze(1)  # TENSOR (B, 1, 1)
        shift = intr[:, 1:].unsqueeze(1)  # TENSOR (B, 1, 2)
        uv = xy * scale + shift  # TENSOR (B, NKP, 2), [0 ~ INP_RES]
        uv = torch.einsum("bij,j->bij", uv, 1.0 / inp_res)  # TENSOR (B, NKP, 2), [0 ~ INP_RES]
        uvd = torch.cat((uv, d.unsqueeze(-1)), -1)  # TENSOR (B, NKP, 3)

    return uvd


def batch_uvd2xyz(
    uvd: torch.Tensor,
    root_joint: torch.Tensor,
    intr: torch.Tensor,
    inp_res: List[int],
    depth_range: float = 0.4,
    ref_bone_len: Optional[torch.Tensor] = None,
    camera_mode="persp",
):
    inp_res = torch.Tensor(inp_res).to(uvd.device)
    batch_size = uvd.shape[0]
    if ref_bone_len is None:
        ref_bone_len = torch.ones((batch_size, 1)).to(uvd.device)

    #  1. denormalized uvd
    uv = torch.einsum("bij,j->bij", uvd[:, :, :2], inp_res)  # TENSOR (B, NKP, 2), [0 ~ INP_RES]
    d = (uvd[:, :, 2] - 0.5) * depth_range  # TENSOR (B, NKP), [-0.2 ~ 0.2]

    if camera_mode == "persp":
        assert intr.dim() == 3, f"Unexpected dim, expect intr has shape (B, 3, 3), got {intr.shape}"
        root_joint_z = root_joint[:, -1].unsqueeze(-1)  # TENSOR (B, 1)
        z = d * ref_bone_len + root_joint_z.expand_as(uvd[:, :, 2])  # TENSOR (B, NKP)

        #  2. uvd->xyz
        # camparam = torch.zeros((batch_size, 4)).float().to(uvd.device)  # TENSOR (B, 4)
        fx = intr[:, 0, 0].unsqueeze(-1)  # TENSOR (B, 1)
        fy = intr[:, 1, 1].unsqueeze(-1)
        cx = intr[:, 0, 2].unsqueeze(-1)
        cy = intr[:, 1, 2].unsqueeze(-1)
        # cat 4 TENSOR (B, 1)
        camparam = torch.cat((fx, fy, cx, cy), dim=1)  # TENSOR (B, 4)
        camparam = camparam.unsqueeze(1).expand(-1, uvd.shape[1], -1)  # TENSOR (B, NKP, 4)
        xy_ = (uv - camparam[:, :, 2:4]) / camparam[:, :, :2]  # TENSOR (B, NKP, 2)
        xy = xy_ * z.unsqueeze(-1).expand_as(uv)  # TENSOR (B, NKP, 2)
        xyz = torch.cat((xy, z.unsqueeze(-1)), -1)  # TENSOR (B, NKP, 3)
    elif camera_mode == "ortho":
        assert intr.dim() == 2, f"Unexpected dim, expect intr has shape (B, 3), got {intr.shape}"
        scale = intr[:, :1].unsqueeze(1)  # TENSOR (B, 1, 1)
        shift = intr[:, 1:].unsqueeze(1)  # TENSOR (B, 1, 2)
        xy = (uv - shift) / scale
        z = d * ref_bone_len
        xyz = torch.cat((xy, z.unsqueeze(-1)), -1)  # TENSOR (B, NKP, 3)

        # add root back
        xyz = xyz + root_joint.unsqueeze(1)  # TENSOR (B, NKP, 3)

    return xyz


def batch_ref_bone_len(joint: Union[np.ndarray, torch.Tensor], ref_bone_link=None) -> Union[np.ndarray, torch.Tensor]:
    if ref_bone_link is None:
        ref_bone_link = (0, 9)

    if not torch.is_tensor(joint) and not isinstance(joint, np.ndarray):
        raise TypeError("joint should be ndarray or torch tensor. Got {}".format(type(joint)))
    if len(joint.shape) != 3 or joint.shape[1] != 21 or joint.shape[2] != 3:
        raise TypeError("joint should have shape (B, njoint, 3), Got {}".format(joint.shape))

    batch_size = joint.shape[0]
    bone = 0
    if torch.is_tensor(joint):
        bone = torch.zeros((batch_size, 1)).to(joint.device)
        for jid, nextjid in zip(ref_bone_link[:-1], ref_bone_link[1:]):
            bone += torch.norm(joint[:, jid, :] - joint[:, nextjid, :], dim=1, keepdim=True)  # (B, 1)
    elif isinstance(joint, np.ndarray):
        bone = np.zeros((batch_size, 1))
        for jid, nextjid in zip(ref_bone_link[:-1], ref_bone_link[1:]):
            bone += np.linalg.norm((joint[:, jid, :] - joint[:, nextjid, :]), ord=2, axis=1, keepdims=True)  # (B, 1)
    return bone


def batch_cam_extr_transf(batch_cam_extr, batch_joints):
    """apply batch camera extrinsic transformation on batch joints

    Args:
        batch_cam_extr (torch.Tensor): shape (BATCH, NPERSP, 4, 4)
        batch_joints (torch.Tensor): shape (BATCH, NPERSP, NJOINTS, 3)

    Returns:
        torch.Tensor: shape (BATCH, NPERSP, NJOINTS, 3)
    """
    res = (batch_cam_extr[..., :3, :3] @ batch_joints.transpose(2, 3)).transpose(2, 3)
    # [B, NPERSP, 3, 3] @ [B, NPERSP, 3, 21] => [B, NPERSP, 3, 21] => [B, NPERSP, 21, 3]
    res = res + batch_cam_extr[..., :3, 3].unsqueeze(2)
    return res


def batch_cam_intr_projection(batch_cam_intr, batch_joints, eps=1e-7):
    """apply camera projection on batch joints with batch intrinsics

    Args:
        batch_cam_intr (torch.Tensor): shape (BATCH, NPERSP, 3, 3)
        batch_joints (torch.Tensor): shape (BATCH, NPERSP, NJOINTS, 3)
        eps (float, optional): avoid divided by zero. Defaults to 1e-7.

    Returns:
        torch.Tensor: shape (BATCH, NPERSP, NJOINTS, 2)
    """
    res = (batch_cam_intr @ batch_joints.transpose(2, 3)).transpose(2, 3)  # [B, NPERSP, 21, 3]
    xy = res[..., 0:2]
    z = res[..., 2:]
    z[torch.abs(z) < eps] = eps
    uv = xy / z
    return uv


def batch_persp_project(verts: torch.Tensor, camintr: torch.Tensor):
    """Batch apply perspective procjection on points

    Args:
        verts (torch.Tensor): 3D points with shape (B, N, 3)
        camintr (torch.Tensor): intrinsic matrix with shape (B, 3, 3)

    Returns:
        torch.Tensor: shape (B, N, 2)
    """
    # Project 3d vertices on image plane
    verts_hom2d = camintr.bmm(verts.transpose(1, 2)).transpose(1, 2)
    proj_verts2d = verts_hom2d[:, :, :2] / verts_hom2d[:, :, 2:]
    return proj_verts2d


def persp_project(points3d, cam_intr):
    hom_2d = np.array(cam_intr).dot(points3d.transpose()).transpose()
    points2d = (hom_2d / (hom_2d[:, 2:] + 1e-6))[:, :2]
    return points2d.astype(np.float32)


def ortho_project(points3d, ortho_cam):
    x, y = points3d[:, 0], points3d[:, 1]
    u = ortho_cam[0] * x + ortho_cam[1]
    v = ortho_cam[0] * y + ortho_cam[2]
    u_, v_ = u[:, np.newaxis], v[:, np.newaxis]
    return np.concatenate([u_, v_], axis=1)


# * >>>>>>>>>>
# * From https://github.com/mkocabas/PARE
def convert_perspective_to_weak_perspective(
    perspective_camera,
    focal_length=5000.0,
    img_res=224,
):
    # Convert Weak Perspective Camera [s, tx, ty] to camera translation [tx, ty, tz]
    # in 3D given the bounding box size
    # This camera translation can be used in a full perspective projection
    # if isinstance(focal_length, torch.Tensor):
    #     focal_length = focal_length[:, 0]

    weak_perspective_camera = torch.stack(
        [
            2 * focal_length / (img_res * perspective_camera[:, 2] + 1e-9),
            perspective_camera[:, 0],
            perspective_camera[:, 1],
        ],
        dim=-1,
    )
    return weak_perspective_camera


def convert_weak_perspective_to_perspective(
    weak_perspective_camera,
    focal_length=5000.0,
    img_res=224,
):
    # Convert Weak Perspective Camera [s, tx, ty] to camera translation [tx, ty, tz]
    # in 3D given the bounding box size
    # This camera translation can be used in a full perspective projection
    # if isinstance(focal_length, torch.Tensor):
    #     focal_length = focal_length[:, 0]

    perspective_camera = torch.stack(
        [
            weak_perspective_camera[:, 1],
            weak_perspective_camera[:, 2],
            2 * focal_length / (img_res * weak_perspective_camera[:, 0] + 1e-9),
        ],
        dim=-1,
    )
    return perspective_camera


def perspective_projection(points, rotation, translation, focal_length, camera_center):
    """
    This function computes the perspective projection of a set of points.
    Input:
        points (bs, N, 3): 3D points
        rotation (bs, 3, 3): Camera rotation
        translation (bs, 3): Camera translation
        focal_length (bs,) or scalar: Focal length
        camera_center (bs, 2): Camera center
    """
    batch_size = points.shape[0]
    K = torch.zeros([batch_size, 3, 3], device=points.device)
    K[:, 0, 0] = focal_length
    K[:, 1, 1] = focal_length
    K[:, 2, 2] = 1.0
    K[:, :-1, -1] = camera_center

    # Transform points
    points = torch.einsum("bij,bkj->bki", rotation, points)
    points = points + translation.unsqueeze(1)

    # Apply perspective distortion
    projected_points = points / points[:, :, -1].unsqueeze(-1)

    # Apply camera intrinsics
    projected_points = torch.einsum("bij,bkj->bki", K, projected_points)

    return projected_points[:, :, :-1]


def weak_perspective_projection(points, rotation, weak_cam_params, focal_length, camera_center, img_res):
    """
    This function computes the perspective projection of a set of points.
    Input:
        points (bs, N, 3): 3D points
        rotation (bs, 3, 3): Camera rotation
        translation (bs, 3): Camera translation
        focal_length (bs,) or scalar: Focal length
        camera_center (bs, 2): Camera center
    """
    batch_size = points.shape[0]
    K = torch.zeros([batch_size, 3, 3], device=points.device)
    K[:, 0, 0] = focal_length
    K[:, 1, 1] = focal_length
    K[:, 2, 2] = 1.0
    K[:, :-1, -1] = camera_center

    translation = convert_weak_perspective_to_perspective(weak_cam_params, focal_length, img_res)

    # Transform points
    points = torch.einsum("bij,bkj->bki", rotation, points)
    points = points + translation.unsqueeze(1)

    # Apply perspective distortion
    projected_points = points / points[:, :, -1].unsqueeze(-1)

    # Apply camera intrinsics
    projected_points = torch.einsum("bij,bkj->bki", K, projected_points)

    return projected_points[:, :, :-1]


# * <<<<<<<<<<


# visible in raw image
def get_verts_2d_vis(verts_2d=None, raw_size=None, **kwargs):
    verts_vis = ((verts_2d[:, 0] >= 0) & (verts_2d[:, 0] < raw_size[0])) & (
        (verts_2d[:, 1] >= 0) & (verts_2d[:, 1] < raw_size[1])
    )
    return verts_vis.astype(np.float32)


def get_annot_scale(annots, visibility=None, scale_factor=1.0):
    """
    Retreives the size of the square we want to crop by taking the
    maximum of vertical and horizontal span of the hand and multiplying
    it by the scale_factor to add some padding around the hand
    """
    if visibility is not None:
        annots = annots[visibility]
    min_x, min_y = annots.min(0)
    max_x, max_y = annots.max(0)
    delta_x = max_x - min_x
    delta_y = max_y - min_y
    max_delta = max(delta_x, delta_y)
    s = max_delta * scale_factor
    return s


def get_annot_center(annots, visibility=None):
    if visibility is not None:
        annots = annots[visibility]
    min_x, min_y = annots.min(0)
    max_x, max_y = annots.max(0)
    c_x = int((max_x + min_x) / 2)
    c_y = int((max_y + min_y) / 2)
    return np.asarray([c_x, c_y])


def bbox_xywh_to_xyxy(xywh):
    """Convert bounding boxes from format (x, y, w, h) to (xmin, ymin, xmax, ymax)

    Parameters
    ----------
    xywh : list, tuple or numpy.ndarray
        The bbox in format (x, y, w, h).
        If numpy.ndarray is provided, we expect multiple bounding boxes with
        shape `(N, 4)`.

    Returns
    -------
    tuple or numpy.ndarray
        The converted bboxes in format (xmin, ymin, xmax, ymax).
        If input is numpy.ndarray, return is numpy.ndarray correspondingly.

    """
    if isinstance(xywh, (tuple, list)):
        if not len(xywh) == 4:
            raise IndexError("Bounding boxes must have 4 elements, given {}".format(len(xywh)))
        w, h = np.maximum(xywh[2] - 1, 0), np.maximum(xywh[3] - 1, 0)
        return (xywh[0], xywh[1], xywh[0] + w, xywh[1] + h)
    elif isinstance(xywh, np.ndarray):
        if not xywh.size % 4 == 0:
            raise IndexError("Bounding boxes must have n * 4 elements, given {}".format(xywh.shape))
        xyxy = np.hstack((xywh[:, :2], xywh[:, :2] + np.maximum(0, xywh[:, 2:4] - 1)))
        return xyxy
    else:
        raise TypeError("Expect input xywh a list, tuple or numpy.ndarray, given {}".format(type(xywh)))


def bbox_xyxy_to_xywh(xyxy):
    """Convert bounding boxes from format (xmin, ymin, xmax, ymax) to (x, y, w, h).

    Parameters
    ----------
    xyxy : list, tuple or numpy.ndarray
        The bbox in format (xmin, ymin, xmax, ymax).
        If numpy.ndarray is provided, we expect multiple bounding boxes with
        shape `(N, 4)`.

    Returns
    -------
    tuple or numpy.ndarray
        The converted bboxes in format (x, y, w, h).
        If input is numpy.ndarray, return is numpy.ndarray correspondingly.

    """
    if isinstance(xyxy, (tuple, list)):
        if not len(xyxy) == 4:
            raise IndexError("Bounding boxes must have 4 elements, given {}".format(len(xyxy)))
        x1, y1 = xyxy[0], xyxy[1]
        w, h = xyxy[2] - x1 + 1, xyxy[3] - y1 + 1
        return (x1, y1, w, h)
    elif isinstance(xyxy, np.ndarray):
        if not xyxy.size % 4 == 0:
            raise IndexError("Bounding boxes must have n * 4 elements, given {}".format(xyxy.shape))
        return np.hstack((xyxy[:, :2], xyxy[:, 2:4] - xyxy[:, :2] + 1))
    else:
        raise TypeError("Expect input xywh a list, tuple or numpy.ndarray, given {}".format(type(xyxy)))


def center_scale_to_box(center, scale):
    """Convert bbox center scale to bbox xyxy

    Args:
        center (np.array): center of the bbox (x, y)
        scale (np.float_): side length of the bbox (bbox must be square)

    Returns:
        list: list of 4 elms, containing bbox' s xmin, ymin, xmax, ymax.
    """
    pixel_std = 1.0
    w = scale * pixel_std
    h = scale * pixel_std
    xmin = center[0] - w * 0.5
    ymin = center[1] - h * 0.5
    xmax = xmin + w
    ymax = ymin + h
    bbox = [xmin, ymin, xmax, ymax]
    return bbox


def denormalize(tensor, mean, std, inplace=False):
    if not isinstance(tensor, torch.Tensor):
        raise TypeError("Input tensor should be a torch tensor. Got {}.".format(type(tensor)))

    if tensor.ndim < 3:
        raise ValueError(
            "Expected tensor to be a tensor image of size (..., C, H, W). Got tensor.size() = "
            "{}.".format(tensor.size())
        )

    dtype = tensor.dtype
    mean = torch.as_tensor(mean, dtype=dtype, device=tensor.device)
    std = torch.as_tensor(std, dtype=dtype, device=tensor.device)
    if (std == 0).any():
        raise ValueError("std evaluated to zero after conversion to {}, leading to division by zero.".format(dtype))
    if mean.ndim == 1:
        mean = mean.view(-1, 1, 1)
    if std.ndim == 1:
        std = std.view(-1, 1, 1)
    tensor.mul_(std).sub_(-1 * mean)
    return tensor


def bhwc_2_bchw(tensor):
    """
    :param x: torch tensor, B x H x W x C
    :return:  torch tensor, B x C x H x W
    """
    if not torch.is_tensor(tensor) or tensor.ndimension() != 4:
        raise TypeError("invalid tensor or tensor channel is not BCHW")
    return tensor.unsqueeze(1).transpose(1, -1).squeeze(-1)


def bchw_2_bhwc(tensor):
    """
    :param x: torch tensor, B x C x H x W
    :return:  torch tensor, B x H x W x C
    """
    if not torch.is_tensor(tensor) or tensor.ndimension() != 4:
        raise TypeError("invalid tensor or tensor channel is not BCHW")
    return tensor.unsqueeze(-1).transpose(1, -1).squeeze(1)


def to_homogeneous_transf(rot, tsl):
    """
    :param rot: torch tensor, B x 3 x 3
    :param tsl: torch tensor, B x 3
    :return:    torch tensor, B x 4 x 4
    """
    if not torch.is_tensor(rot) or not torch.is_tensor(tsl):
        raise TypeError("invalid tensor")
    if rot.ndimension() != 3 or tsl.ndimension() != 2:
        raise TypeError("invalid tensor dimension")
    if rot.shape[0] != tsl.shape[0]:
        raise TypeError("invalid tensor dimension")
    if rot.shape[1] != 3 or rot.shape[2] != 3:
        raise TypeError("invalid tensor dimension")
    if tsl.shape[1] != 3:
        raise TypeError("invalid tensor dimension")
    tsl = tsl.unsqueeze(-1)
    transf = torch.cat((rot, tsl), dim=-1)
    transf = torch.cat(
        (transf, torch.tensor([0, 0, 0, 1], device=rot.device).view(1, 1, 4).repeat(rot.shape[0], 1, 1)), dim=1
    )
    return transf


def center_vert_bbox(vertices, bbox_center=None, bbox_scale=None, scale=False):
    if bbox_center is None:
        bbox_center = (vertices.min(0) + vertices.max(0)) / 2
    vertices = vertices - bbox_center
    if scale:
        if bbox_scale is None:
            bbox_scale = np.linalg.norm(vertices, 2, 1).max()
        vertices = vertices / bbox_scale
    else:
        bbox_scale = 1
    return vertices, bbox_center, bbox_scale


def generate_rand_transf(batch_size, r_angle=np.pi / 2, r_tsl=0.1, device="cpu"):
    rot = R.random(batch_size).as_rotvec()
    rot = rot / np.linalg.norm(rot, axis=1, keepdims=True)
    angle = np.random.rand(batch_size) * (r_angle * 2) - r_angle  # angle between -r_angle and r_angle
    rot = rot * angle[:, None]

    rot = torch.from_numpy(rot).float().to(device)
    rot = aa_to_rotmat(rot)

    tsl = torch.rand(batch_size, 3).float().to(device) * r_tsl * 2 - r_tsl  # tsl between -r_tsl and r_tsl

    transf = torch.cat([rot, tsl[:, :, None]], dim=2)
    transf = torch.cat(
        [
            transf,
            torch.tensor([0, 0, 0, 1], dtype=torch.float32).to(device)[None, None, :].repeat(batch_size, 1, 1),
        ],
        dim=1,
    )
    return transf


def caculate_align_mat(p1, p2):
    vec = p2 - p1
    vec = vec / np.linalg.norm(vec)
    z_unit_Arr = np.array([0, 0, 1])

    z_mat = np.array(
        [
            [0, -z_unit_Arr[2], z_unit_Arr[1]],
            [z_unit_Arr[2], 0, -z_unit_Arr[0]],
            [-z_unit_Arr[1], z_unit_Arr[0], 0],
        ]
    )

    z_c_vec = np.matmul(z_mat, vec)
    z_c_vec_mat = np.array(
        [
            [0, -z_c_vec[2], z_c_vec[1]],
            [z_c_vec[2], 0, -z_c_vec[0]],
            [-z_c_vec[1], z_c_vec[0], 0],
        ]
    )

    if np.dot(z_unit_Arr, vec) == -1:
        qTrans_Mat = -np.eye(3, 3)
    elif np.dot(z_unit_Arr, vec) == 1:
        qTrans_Mat = np.eye(3, 3)
    else:
        qTrans_Mat = np.eye(3, 3) + z_c_vec_mat + np.matmul(z_c_vec_mat, z_c_vec_mat) / (1 + np.dot(z_unit_Arr, vec))

    return qTrans_Mat


def slerp_and_lerp_pose(matrix1, matrix2, t):
    rotation1 = matrix1[:3, :3]
    rotation2 = matrix2[:3, :3]

    translation1 = matrix1[:3, 3]
    translation2 = matrix2[:3, 3]

    slerp = Slerp([0, 1], R.concatenate([R.from_matrix(rotation1), R.from_matrix(rotation2)]))

    slerp_rotation = slerp(t).as_matrix()  # [t, 3, 3]

    lerp_translation = []
    for i in t:
        lerp_t = (1 - i) * translation1 + i * translation2  # [3]
        lerp_translation.append(lerp_t)
    lerp_translation = np.stack(lerp_translation, axis=0)  # [t, 3]

    result = np.eye(4)[None].repeat(len(t), axis=0)  # [t, 4, 4]
    result[:, :3, :3] = slerp_rotation
    result[:, :3, 3] = lerp_translation

    return result.astype(np.float32)


class P2PSigned(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        import chamfer_distance as chd

        self.ch_dist = chd.ChamferDistance()

    def forward(
        self,
        x,
        y,
        x_normals=None,
        y_normals=None,
        return_vector=False,
    ):
        """
        signed distance between two pointclouds

        Args:
            x: FloatTensor of shape (N, P1, D) representing a batch of point clouds
                with P1 points in each batch element, batch size N and feature
                dimension D.
            y: FloatTensor of shape (N, P2, D) representing a batch of point clouds
                with P2 points in each batch element, batch size N and feature
                dimension D.
            x_normals: Optional FloatTensor of shape (N, P1, D).
            y_normals: Optional FloatTensor of shape (N, P2, D).

        Returns:

            - y2x_signed: Torch.Tensor
                the sign distance from y to x
            - y2x_signed: Torch.Tensor
                the sign distance from y to x
            - yidx_near: Torch.tensor
                the indices of x vertices closest to y

        """

        N, P1, D = x.shape
        P2 = y.shape[1]

        if y.shape[0] != N or y.shape[2] != D:
            raise ValueError("y does not have the correct shape.")

        x_near, y_near, xidx_near, yidx_near = self.ch_dist(x, y)

        xidx_near_expanded = xidx_near.view(N, P1, 1).expand(N, P1, D).to(torch.long)
        x_near = y.gather(1, xidx_near_expanded)

        yidx_near_expanded = yidx_near.view(N, P2, 1).expand(N, P2, D).to(torch.long)
        y_near = x.gather(1, yidx_near_expanded)

        x2y = x - x_near  # y point to x
        y2x = y - y_near  # x point to y

        if x_normals is not None:
            y_nn = x_normals.gather(1, yidx_near_expanded)
            in_out = torch.bmm(y_nn.reshape(-1, 1, 3), y2x.reshape(-1, 3, 1)).view(N, -1).sign()
            y2x_signed = y2x.norm(dim=2) * in_out

        else:
            y2x_signed = y2x.norm(dim=2)

        if y_normals is not None:
            x_nn = y_normals.gather(1, xidx_near_expanded)
            in_out_x = torch.bmm(x_nn.reshape(-1, 1, 3), x2y.reshape(-1, 3, 1)).view(N, -1).sign()
            x2y_signed = x2y.norm(dim=2) * in_out_x
        else:
            x2y_signed = x2y.norm(dim=2)

        if not return_vector:
            return y2x_signed, x2y_signed, yidx_near, xidx_near
        else:
            return y2x_signed, x2y_signed, yidx_near, xidx_near, y2x, x2y
