import torch
import torchvision.transforms as T
import torchvision.transforms.functional as F
import torch.nn.functional as NNF
import torch.nn.functional as NNF
from PIL import Image, ImageSequence, ImageOps
from PIL.PngImagePlugin import PngInfo
import random
import folder_paths
import hashlib
import numpy as np
import os
from pathlib import Path
from comfy.cli_args import args
from comfy_extras import nodes_mask as masks
import comfy.utils
import nodes as nodes
import json
import math
import datetime

yanc_root_name = "YANC"
yanc_sub_image = "/😼 Image"
yanc_sub_text = "/😼 Text"
yanc_sub_basics = "/😼 Basics"
yanc_sub_nik = "/😼 Noise Injection Sampler"
yanc_sub_masking = "/😼 Masking"
yanc_sub_utils = "/😼 Utils"

# ------------------------------------------------------------------------------------------------------------------ #
# Functions                                                                                                          #
# ------------------------------------------------------------------------------------------------------------------ #


def permute_to_image(image):
    image = T.ToTensor()(image).unsqueeze(0)
    return image.permute([0, 2, 3, 1])[:, :, :, :3]


def to_binary_mask(image):
    images_sum = image.sum(axis=3)
    return torch.where(images_sum > 0, 1.0, 0.)


def print_brown(text):
    print("\033[33m" + text + "\033[0m")


def print_cyan(text):
    print("\033[96m" + text + "\033[0m")


def print_green(text):
    print("\033[92m" + text + "\033[0m")


def get_common_aspect_ratios():
    return [
        (4, 3),
        (3, 2),
        (16, 9),
        (1, 1),
        (21, 9),
        (9, 16),
        (3, 4),
        (2, 3),
        (5, 8)
    ]


def get_sdxl_resolutions():
    return [
        ("1:1", (1024, 1024)),
        ("3:4", (896, 1152)),
        ("5:8", (832, 1216)),
        ("9:16", (768, 1344)),
        ("9:21", (640, 1536)),
        ("4:3", (1152, 896)),
        ("3:2", (1216, 832)),
        ("16:9", (1344, 768)),
        ("21:9", (1536, 640))
    ]


def get_15_resolutions():
    return [
        ("1:1", (512, 512)),
        ("2:3", (512, 768)),
        ("3:4", (512, 682)),
        ("3:2", (768, 512)),
        ("16:9", (910, 512)),
        ("1.85:1", (952, 512)),
        ("2:1", (1024, 512)),
        ("2.39:1", (1224, 512))
    ]


def replace_dt_placeholders(string):
    dt = datetime.datetime.now()

    format_mapping = {
        "%d",  # Day
        "%m",  # Month
        "%Y",  # Year long
        "%y",  # Year short
        "%H",  # Hour 00 - 23
        "%I",  # Hour 00 - 12
        "%p",  # AM/PM
        "%M",  # Minute
        "%S"  # Second
    }

    for placeholder in format_mapping:
        if placeholder in string:
            string = string.replace(placeholder, dt.strftime(placeholder))

    return string


def patch(model, multiplier):  # RescaleCFG functionality from the ComfyUI nodes
    def rescale_cfg(args):
        cond = args["cond"]
        uncond = args["uncond"]
        cond_scale = args["cond_scale"]
        sigma = args["sigma"]
        sigma = sigma.view(sigma.shape[:1] + (1,) * (cond.ndim - 1))
        x_orig = args["input"]

        # rescale cfg has to be done on v-pred model output
        x = x_orig / (sigma * sigma + 1.0)
        cond = ((x - (x_orig - cond)) * (sigma ** 2 + 1.0) ** 0.5) / (sigma)
        uncond = ((x - (x_orig - uncond)) *
                  (sigma ** 2 + 1.0) ** 0.5) / (sigma)

        # rescalecfg
        x_cfg = uncond + cond_scale * (cond - uncond)
        ro_pos = torch.std(cond, dim=(1, 2, 3), keepdim=True)
        ro_cfg = torch.std(x_cfg, dim=(1, 2, 3), keepdim=True)

        x_rescaled = x_cfg * (ro_pos / ro_cfg)
        x_final = multiplier * x_rescaled + (1.0 - multiplier) * x_cfg

        return x_orig - (x - x_final * sigma / (sigma * sigma + 1.0) ** 0.5)

    m = model.clone()
    m.set_model_sampler_cfg_function(rescale_cfg)
    return (m, )


def blend_images(image1, image2, blend_mode, blend_rate):
    if blend_mode == 'multiply':
        return (1 - blend_rate) * image1 + blend_rate * (image1 * image2)
    elif blend_mode == 'add':
        return (1 - blend_rate) * image1 + blend_rate * (image1 + image2)
    elif blend_mode == 'overlay':
        blended_image = torch.where(
            image1 < 0.5, 2 * image1 * image2, 1 - 2 * (1 - image1) * (1 - image2))
        return (1 - blend_rate) * image1 + blend_rate * blended_image
    elif blend_mode == 'soft light':
        return (1 - blend_rate) * image1 + blend_rate * (soft_light_blend(image1, image2))
    elif blend_mode == 'hard light':
        return (1 - blend_rate) * image1 + blend_rate * (hard_light_blend(image1, image2))
    elif blend_mode == 'lighten':
        return (1 - blend_rate) * image1 + blend_rate * (lighten_blend(image1, image2))
    elif blend_mode == 'darken':
        return (1 - blend_rate) * image1 + blend_rate * (darken_blend(image1, image2))
    else:
        raise ValueError("Unsupported blend mode")


def soft_light_blend(base, blend):
    return 2 * base * blend + base**2 * (1 - 2 * blend)


def hard_light_blend(base, blend):
    return 2 * base * blend + (1 - 2 * base) * (1 - blend)


def lighten_blend(base, blend):
    return torch.max(base, blend)


def darken_blend(base, blend):
    return torch.min(base, blend)


# ------------------------------------------------------------------------------------------------------------------ #
# Comfy classes                                                                                                      #
# ------------------------------------------------------------------------------------------------------------------ #
class YANCRotateImage:
    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "image": ("IMAGE",),
                "rotation_angle": ("INT", {
                    "default": 0,
                    "min": -359,
                    "max": 359,
                    "step": 1,
                    "display": "number"})
            },
        }

    RETURN_TYPES = ("IMAGE", "MASK")
    RETURN_NAMES = ("image", "mask")

    FUNCTION = "do_it"

    CATEGORY = yanc_root_name + yanc_sub_image

    def do_it(self, image, rotation_angle):
        samples = image.movedim(-1, 1)
        height, width = F.get_image_size(samples)

        rotation_angle = rotation_angle * -1
        rotated_image = F.rotate(samples, angle=rotation_angle, expand=True)

        empty_mask = Image.new('RGBA', (height, width), color=(255, 255, 255))
        rotated_mask = F.rotate(empty_mask, angle=rotation_angle, expand=True)

        img_out = rotated_image.movedim(1, -1)
        mask_out = to_binary_mask(permute_to_image(rotated_mask))

        return (img_out, mask_out)

# ------------------------------------------------------------------------------------------------------------------ #


class YANCText:
    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "text": ("STRING", {
                    "multiline": True,
                    "default": "",
                    "dynamicPrompts": True
                }),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("text",)

    FUNCTION = "do_it"

    CATEGORY = yanc_root_name + yanc_sub_text

    def do_it(self, text):
        return (text,)

# ------------------------------------------------------------------------------------------------------------------ #


class YANCTextCombine:
    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "text": ("STRING", {"forceInput": True}),
                "text_append": ("STRING", {"forceInput": True}),
                "delimiter": ("STRING", {"multiline": False, "default": ", "}),
                "add_empty_line": ("BOOLEAN", {"default": False})
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("text",)

    FUNCTION = "do_it"

    CATEGORY = yanc_root_name + yanc_sub_text

    def do_it(self, text, text_append, delimiter, add_empty_line):
        if text_append.strip() == "":
            delimiter = ""

        str_list = [text, text_append]

        if add_empty_line:
            str_list = [text, "\n\n", text_append]

        return (delimiter.join(str_list),)

# ------------------------------------------------------------------------------------------------------------------ #


class YANCTextPickRandomLine:
    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "text": ("STRING", {"forceInput": True}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff})
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("text",)

    FUNCTION = "do_it"

    CATEGORY = yanc_root_name + yanc_sub_text

    def do_it(self, text, seed):
        lines = text.splitlines()
        random.seed(seed)
        line = random.choice(lines)

        return (line,)

# ------------------------------------------------------------------------------------------------------------------ #


class YANCClearText:
    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "text": ("STRING", {"forceInput": True}),
                "chance": ("FLOAT", {
                    "default": 0.0,
                    "min": 0.0,
                    "max": 1.0,
                    "step": 0.01,
                    "round": 0.001,
                    "display": "number"}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("text",)

    FUNCTION = "do_it"

    CATEGORY = yanc_root_name + yanc_sub_text

    def do_it(self, text, chance):
        dice = random.uniform(0, 1)

        if chance > dice:
            text = ""

        return (text,)

    @classmethod
    def IS_CHANGED(s, text, chance):
        return s.do_it(s, text, chance)

# ------------------------------------------------------------------------------------------------------------------ #


class YANCTextReplace:
    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "text": ("STRING", {"forceInput": True}),
                "find": ("STRING", {
                    "multiline": False,
                    "Default": "find"
                }),
                "replace": ("STRING", {
                    "multiline": False,
                    "Default": "replace"
                }),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("text",)

    FUNCTION = "do_it"

    CATEGORY = yanc_root_name + yanc_sub_text

    def do_it(self, text, find, replace):
        text = text.replace(find, replace)

        return (text,)

# ------------------------------------------------------------------------------------------------------------------ #


class YANCTextRandomWeights:
    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "text": ("STRING", {"forceInput": True}),
                "min": ("FLOAT", {
                    "default": 1.0,
                    "min": 0.0,
                    "max": 10.0,
                    "step": 0.1,
                    "round": 0.1,
                    "display": "number"}),
                "max": ("FLOAT", {
                    "default": 1.0,
                    "min": 0.0,
                    "max": 10.0,
                    "step": 0.1,
                    "round": 0.1,
                    "display": "number"}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("text",)

    FUNCTION = "do_it"

    CATEGORY = yanc_root_name + yanc_sub_text

    def do_it(self, text, min, max, seed):
        lines = text.splitlines()
        count = 0
        out = ""

        random.seed(seed)

        for line in lines:
            count += 1
            out += "({}:{})".format(line, round(random.uniform(min, max), 1)
                                    ) + (", " if count < len(lines) else "")

        return (out,)

# ------------------------------------------------------------------------------------------------------------------ #


class YANCLoadImageAndFilename:
    @classmethod
    def INPUT_TYPES(s):
        input_dir = folder_paths.get_input_directory()
        files = [f for f in os.listdir(input_dir) if os.path.isfile(
            os.path.join(input_dir, f))]
        return {"required":
                {"image": (sorted(files), {"image_upload": True}),
                 "strip_extension": ("BOOLEAN", {"default": True})}
                }

    CATEGORY = yanc_root_name + yanc_sub_image

    RETURN_TYPES = ("IMAGE", "MASK", "STRING")
    RETURN_NAMES = ("IMAGE", "MASK", "FILENAME")

    FUNCTION = "do_it"

    def do_it(self, image, strip_extension):
        image_path = folder_paths.get_annotated_filepath(image)
        img = Image.open(image_path)
        output_images = []
        output_masks = []
        for i in ImageSequence.Iterator(img):
            i = ImageOps.exif_transpose(i)
            if i.mode == 'I':
                i = i.point(lambda i: i * (1 / 255))
            image = i.convert("RGB")
            image = np.array(image).astype(np.float32) / 255.0
            image = torch.from_numpy(image)[None,]
            if 'A' in i.getbands():
                mask = np.array(i.getchannel('A')).astype(np.float32) / 255.0
                mask = 1. - torch.from_numpy(mask)
            else:
                mask = torch.zeros((64, 64), dtype=torch.float32, device="cpu")
            output_images.append(image)
            output_masks.append(mask.unsqueeze(0))

        if len(output_images) > 1:
            output_image = torch.cat(output_images, dim=0)
            output_mask = torch.cat(output_masks, dim=0)
        else:
            output_image = output_images[0]
            output_mask = output_masks[0]

        if strip_extension:
            filename = Path(image_path).stem
        else:
            filename = Path(image_path).name

        return (output_image, output_mask, filename,)

    @classmethod
    def IS_CHANGED(s, image, strip_extension):
        image_path = folder_paths.get_annotated_filepath(image)
        m = hashlib.sha256()
        with open(image_path, 'rb') as f:
            m.update(f.read())
        return m.digest().hex()

    @classmethod
    def VALIDATE_INPUTS(s, image, strip_extension):
        if not folder_paths.exists_annotated_filepath(image):
            return "Invalid image file: {}".format(image)

        return True

# ------------------------------------------------------------------------------------------------------------------ #


class YANCSaveImage:
    def __init__(self):
        self.output_dir = folder_paths.get_output_directory()
        self.type = "output"
        self.prefix_append = ""
        self.compress_level = 4

    @classmethod
    def INPUT_TYPES(s):
        return {"required":
                {"images": ("IMAGE", ),
                 "filename_prefix": ("STRING", {"default": "ComfyUI"}),
                 "folder": ("STRING", {"default": ""}),
                 "overwrite_warning": ("BOOLEAN", {"default": False}),
                 "include_metadata": ("BOOLEAN", {"default": True}),
                 "extension": (["png", "jpg"],),
                 "quality": ("INT", {"default": 95, "min": 0, "max": 100}),
                 },
                "optional":
                    {"filename_opt": ("STRING", {"forceInput": True})},
                "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},
                }

    RETURN_TYPES = ()
    FUNCTION = "do_it"

    OUTPUT_NODE = True

    CATEGORY = yanc_root_name + yanc_sub_image

    def do_it(self, images, overwrite_warning, include_metadata, extension, quality, filename_opt=None, folder=None, filename_prefix="ComfyUI", prompt=None, extra_pnginfo=None,):

        if folder:
            filename_prefix += self.prefix_append
            filename_prefix = os.sep.join([folder, filename_prefix])
        else:
            filename_prefix += self.prefix_append

        if "%" in filename_prefix:
            filename_prefix = replace_dt_placeholders(filename_prefix)

        full_output_folder, filename, counter, subfolder, filename_prefix = folder_paths.get_save_image_path(
            filename_prefix, self.output_dir, images[0].shape[1], images[0].shape[0])

        results = list()
        for (batch_number, image) in enumerate(images):
            i = 255. * image.cpu().numpy()
            img = Image.fromarray(np.clip(i, 0, 255).astype(np.uint8))
            metadata = None

            if not filename_opt:

                filename_with_batch_num = filename.replace(
                    "%batch_num%", str(batch_number))

                counter = 1

                if os.path.exists(full_output_folder) and os.listdir(full_output_folder):
                    filtered_filenames = list(filter(
                        lambda filename: filename.startswith(
                            filename_with_batch_num + "_")
                        and filename[len(filename_with_batch_num) + 1:-4].isdigit(),
                        os.listdir(full_output_folder)
                    ))

                    if filtered_filenames:
                        max_counter = max(
                            int(filename[len(filename_with_batch_num) + 1:-4])
                            for filename in filtered_filenames
                        )
                        counter = max_counter + 1

                file = f"{filename_with_batch_num}_{counter:05}.{extension}"
            else:
                if len(images) == 1:
                    file = f"{filename_opt}.{extension}"
                else:
                    raise Exception(
                        "Multiple images and filename detected: Images will overwrite themselves!")

            save_path = os.path.join(full_output_folder, file)

            if os.path.exists(save_path) and overwrite_warning:
                raise Exception("Filename already exists.")
            else:
                if extension == "png":
                    if not args.disable_metadata and include_metadata:
                        metadata = PngInfo()
                        if prompt is not None:
                            metadata.add_text("prompt", json.dumps(prompt))
                        if extra_pnginfo is not None:
                            for x in extra_pnginfo:
                                metadata.add_text(x, json.dumps(extra_pnginfo[x]))

                    img.save(save_path, pnginfo=metadata,
                            compress_level=self.compress_level)
                elif extension == "jpg":
                    if not args.disable_metadata and include_metadata:
                        metadata = {}

                        if prompt is not None:
                            metadata["prompt"] = prompt
                        if extra_pnginfo is not None:
                            for key, value in extra_pnginfo.items():
                                metadata[key] = value

                        metadata_json = json.dumps(metadata)
                        img.info["comment"] = metadata_json

                    img.save(save_path, quality=quality)

            results.append({
                "filename": file,
                "subfolder": subfolder,
                "type": self.type
            })

        return {"ui": {"images": results}}

# ------------------------------------------------------------------------------------------------------------------ #


class YANCLoadImageFromFolder:
    @classmethod
    def INPUT_TYPES(s):
        return {"required":
                {"image_folder": ("STRING", {"default": ""})
                 },
                "optional":
                    {"index": ("INT",
                               {"default": -1,
                                "min": -1,
                                "max": 0xffffffffffffffff,
                                "forceInput": True})}
                }

    CATEGORY = yanc_root_name + yanc_sub_image

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("image", "file_name")
    FUNCTION = "do_it"

    def do_it(self, image_folder, index=-1):

        image_path = os.path.join(
            folder_paths.get_input_directory(), image_folder)

        # Get all files in the directory
        files = os.listdir(image_path)

        # Filter out only image files
        image_files = [file for file in files if file.endswith(
            ('.jpg', '.jpeg', '.png', '.webp'))]

        if index is not -1:
            print_green("INFO: Index connected.")

            if index > len(image_files) - 1:
                index = index % len(image_files)
                print_green(
                    "INFO: Index too high, falling back to: " + str(index))

            image_file = image_files[index]
        else:
            print_green("INFO: Picking a random image.")
            image_file = random.choice(image_files)

        filename = Path(image_file).stem

        img_path = os.path.join(image_path, image_file)

        img = Image.open(img_path)
        img = ImageOps.exif_transpose(img)
        if img.mode == 'I':
            img = img.point(lambda i: i * (1 / 255))
        output_image = img.convert("RGB")
        output_image = np.array(output_image).astype(np.float32) / 255.0
        output_image = torch.from_numpy(output_image)[None,]

        return (output_image, filename)

    @classmethod
    def IS_CHANGED(s, image_folder, index):
        image_path = folder_paths.get_input_directory()
        m = hashlib.sha256()
        with open(image_path, 'rb') as f:
            m.update(f.read())
        return m.digest().hex()

# ------------------------------------------------------------------------------------------------------------------ #


class YANCIntToText:
    @classmethod
    def INPUT_TYPES(s):
        return {"required":
                {"int": ("INT",
                         {"default": 0,
                          "min": 0,
                          "max": 0xffffffffffffffff,
                          "forceInput": True}),
                 "leading_zeros": ("BOOLEAN", {"default": False}),
                 "length": ("INT",
                            {"default": 5,
                             "min": 0,
                             "max": 5})
                 }
                }

    CATEGORY = yanc_root_name + yanc_sub_basics

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("text",)
    FUNCTION = "do_it"

    def do_it(self, int, leading_zeros, length):

        text = str(int)

        if leading_zeros:
            text = text.zfill(length)

        return (text,)

# ------------------------------------------------------------------------------------------------------------------ #


class YANCInt:
    @classmethod
    def INPUT_TYPES(s):
        return {"required":
                {"seed": ("INT", {"default": 0, "min": 0,
                          "max": 0xffffffffffffffff}), }
                }

    CATEGORY = yanc_root_name + yanc_sub_basics

    RETURN_TYPES = ("INT",)
    RETURN_NAMES = ("int",)
    FUNCTION = "do_it"

    def do_it(self, seed):

        return (seed,)

# ------------------------------------------------------------------------------------------------------------------ #


class YANCFloatToInt:
    @classmethod
    def INPUT_TYPES(s):
        return {"required":
                {"float": ("FLOAT", {"forceInput": True}),
                 "function": (["round", "floor", "ceil"],)
                 }
                }

    CATEGORY = yanc_root_name + yanc_sub_basics

    RETURN_TYPES = ("INT",)
    RETURN_NAMES = ("int",)
    FUNCTION = "do_it"

    def do_it(self, float, function):

        result = round(float)

        if function == "floor":
            result = math.floor(float)
        elif function == "ceil":
            result = math.ceil(float)

        return (int(result),)

# ------------------------------------------------------------------------------------------------------------------ #


class YANCScaleImageToSide:
    @classmethod
    def INPUT_TYPES(s):
        return {"required":
                {
                    "image": ("IMAGE",),
                    "scale_to": ("INT", {"default": 512}),
                    "side": (["shortest", "longest", "width", "height"],),
                    "interpolation": (["lanczos", "nearest", "bilinear", "bicubic", "area", "nearest-exact"],),
                    "modulo": ("INT", {"default": 0})
                },
                "optional":
                {
                    "mask_opt": ("MASK",),
                }
                }

    CATEGORY = yanc_root_name + yanc_sub_image

    RETURN_TYPES = ("IMAGE", "MASK", "INT", "INT", "FLOAT",)
    RETURN_NAMES = ("image", "mask", "width", "height", "scale_ratio",)
    FUNCTION = "do_it"

    def do_it(self, image, scale_to, side, interpolation, modulo, mask_opt=None):

        image = image.movedim(-1, 1)

        image_height, image_width = image.shape[-2:]

        longer_side = "height" if image_height > image_width else "width"
        shorter_side = "height" if image_height < image_width else "width"

        new_height, new_width, scale_ratio = 0, 0, 0

        if side == "shortest":
            side = shorter_side
        elif side == "longest":
            side = longer_side

        if side == "width":
            scale_ratio = scale_to / image_width
        elif side == "height":
            scale_ratio = scale_to / image_height

        new_height = image_height * scale_ratio
        new_width = image_width * scale_ratio

        if modulo != 0:
            new_height = new_height - (new_height % modulo)
            new_width = new_width - (new_width % modulo)

        new_width = int(new_width)
        new_height = int(new_height)

        image = comfy.utils.common_upscale(image,
                                           new_width, new_height, interpolation, "center")

        if mask_opt is not None:
            mask_opt = mask_opt.permute(0, 1, 2)

            mask_opt = mask_opt.unsqueeze(0)
            mask_opt = NNF.interpolate(mask_opt, size=(
                new_height, new_width), mode='bilinear', align_corners=False)

            mask_opt = mask_opt.squeeze(0)
            mask_opt = mask_opt.squeeze(0)

            mask_opt = mask_opt.permute(0, 1)

        image = image.movedim(1, -1)

        return (image, mask_opt, new_width, new_height, scale_ratio)

# ------------------------------------------------------------------------------------------------------------------ #


class YANCResolutionByAspectRatio:
    @classmethod
    def INPUT_TYPES(s):
        return {"required":
                {
                    "stable_diffusion": (["1.5", "SDXL"],),
                    "image": ("IMAGE",),
                },
                }

    CATEGORY = yanc_root_name + yanc_sub_image

    RETURN_TYPES = ("INT", "INT")
    RETURN_NAMES = ("width", "height",)
    FUNCTION = "do_it"

    def do_it(self, stable_diffusion, image):

        common_ratios = get_common_aspect_ratios()
        resolutionsSDXL = get_sdxl_resolutions()
        resolutions15 = get_15_resolutions()

        resolution = resolutions15 if stable_diffusion == "1.5" else resolutionsSDXL

        image_height, image_width = 0, 0

        image = image.movedim(-1, 1)
        image_height, image_width = image.shape[-2:]

        gcd = math.gcd(image_width, image_height)
        aspect_ratio = image_width // gcd, image_height // gcd

        closest_ratio = min(common_ratios, key=lambda x: abs(
            x[1] / x[0] - aspect_ratio[1] / aspect_ratio[0]))

        closest_resolution = min(resolution, key=lambda res: abs(
            res[1][0] * aspect_ratio[1] - res[1][1] * aspect_ratio[0]))

        height, width = closest_resolution[1][1], closest_resolution[1][0]
        sd_version = stable_diffusion if stable_diffusion == "SDXL" else "SD 1.5"

        print_cyan(
            f"Orig. Resolution: {image_width}x{image_height}, Aspect Ratio: {closest_ratio[0]}:{closest_ratio[1]}, Picked resolution: {width}x{height} for {sd_version}")

        return (width, height,)

# ------------------------------------------------------------------------------------------------------------------ #


class YANCNIKSampler:
    @classmethod
    def INPUT_TYPES(s):
        return {"required":
                {"model": ("MODEL",),
                 "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                 "steps": ("INT", {"default": 30, "min": 1, "max": 10000}),
                 "cfg": ("FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0, "step": 0.1, "round": 0.01}),
                 "cfg_noise": ("FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0, "step": 0.1, "round": 0.01}),
                 "sampler_name": (comfy.samplers.KSampler.SAMPLERS, ),
                 "scheduler": (comfy.samplers.KSampler.SCHEDULERS, ),
                 "positive": ("CONDITIONING", ),
                 "negative": ("CONDITIONING", ),
                 "latent_image": ("LATENT", ),
                 "noise_strength": ("FLOAT", {"default": 0.5, "min": 0.1, "max": 1.0, "step": 0.1, "round": 0.01}),
                 },
                "optional":
                {
                    "latent_noise": ("LATENT", ),
                    "mask": ("MASK",)
                }
                }

    RETURN_TYPES = ("LATENT",)
    RETURN_NAME = ("latent",)
    FUNCTION = "do_it"

    CATEGORY = yanc_root_name + yanc_sub_nik

    def do_it(self, model, seed, steps, cfg, cfg_noise, sampler_name, scheduler, positive, negative, latent_image, noise_strength, latent_noise, inject_time=0.5, denoise=1.0, mask=None):

        inject_at_step = round(steps * inject_time)
        print("Inject at step: " + str(inject_at_step))

        empty_latent = False if torch.all(
            latent_image["samples"]) != 0 else True

        print_cyan("Sampling first step image.")
        samples_base_sampler = nodes.common_ksampler(model, seed, steps, cfg, sampler_name, scheduler, positive, negative, latent_image,
                                                     denoise=denoise, disable_noise=False, start_step=0, last_step=inject_at_step, force_full_denoise=True)

        if mask is not None and empty_latent:
            print_cyan(
                "Sampling full image for unmasked areas. You can avoid this step by providing a non empty latent.")
            samples_base_sampler2 = nodes.common_ksampler(
                model, seed, steps, cfg, sampler_name, scheduler, positive, negative, latent_image, denoise=1.0)

        samples_base_sampler = samples_base_sampler[0]

        if mask is not None and not empty_latent:
            samples_base_sampler = latent_image.copy()
            samples_base_sampler["samples"] = latent_image["samples"].clone()

        samples_out = latent_image.copy()
        samples_out["samples"] = latent_image["samples"].clone()

        samples_noise = latent_noise.copy()
        samples_noise = latent_noise["samples"].clone()

        if samples_base_sampler["samples"].shape != samples_noise.shape:
            samples_noise.permute(0, 3, 1, 2)
            samples_noise = comfy.utils.common_upscale(
                samples_noise, samples_base_sampler["samples"].shape[3], samples_base_sampler["samples"].shape[2], 'bicubic', crop='center')
            samples_noise.permute(0, 2, 3, 1)

        samples_o = samples_base_sampler["samples"] * (1 - noise_strength)
        samples_n = samples_noise * noise_strength

        samples_out["samples"] = samples_o + samples_n

        patched_model = patch(model=model, multiplier=0.65)[
            0] if round(cfg_noise, 1) > 8.0 else model

        print_cyan("Applying noise.")
        result = nodes.common_ksampler(patched_model, seed, steps, cfg_noise, sampler_name, scheduler, positive, negative, samples_out,
                                       denoise=denoise, disable_noise=False, start_step=inject_at_step, last_step=steps, force_full_denoise=False)[0]

        if mask is not None:
            print_cyan("Composing...")
            destination = latent_image["samples"].clone(
            ) if not empty_latent else samples_base_sampler2[0]["samples"].clone()
            source = result["samples"]
            result["samples"] = masks.composite(
                destination, source, 0, 0, mask, 8)

        return (result,)

# ------------------------------------------------------------------------------------------------------------------ #


class YANCNoiseFromImage:
    @classmethod
    def INPUT_TYPES(s):
        return {"required":
                {
                    "image": ("IMAGE",),
                    "magnitude": ("FLOAT", {"default": 210.0, "min": 0.0, "max": 250.0, "step": 0.5, "round": 0.1}),
                    "smoothness": ("FLOAT", {"default": 3.0, "min": 0.0, "max": 10.0, "step": 0.5, "round": 0.1}),
                    "noise_intensity": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01, "round": 0.01}),
                    "noise_resize_factor": ("INT", {"default": 2.0, "min": 0, "max": 5.0}),
                    "noise_blend_rate": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 1.0, "step": 0.005, "round": 0.005}),
                    "saturation_correction": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.5, "step": 0.1, "round": 0.1}),
                    "blend_mode": (["off", "multiply", "add", "overlay", "soft light", "hard light", "lighten", "darken"],),
                    "blend_rate": ("FLOAT", {"default": 0.25, "min": 0.0, "max": 1.0, "step": 0.01, "round": 0.01}),
                },
                "optional":
                {
                    "vae_opt": ("VAE", ),
                }
                }

    CATEGORY = yanc_root_name + yanc_sub_nik

    RETURN_TYPES = ("IMAGE", "LATENT")
    RETURN_NAMES = ("image", "latent")
    FUNCTION = "do_it"

    def do_it(self, image, magnitude, smoothness, noise_intensity, noise_resize_factor, noise_blend_rate, saturation_correction, blend_mode, blend_rate, vae_opt=None):

        # magnitude:                The alpha for the elastic transform. Magnitude of displacements.
        # smoothness:               The sigma for the elastic transform. Smoothness of displacements.
        # noise_intensity:          Multiplier for the torch noise.
        # noise_resize_factor:      Multiplier to enlarge the generated noise.
        # noise_blend_rate:         Blend rate between the elastic and the noise.
        # saturation_correction:    Well, for saturation correction.
        # blend_mode:               Different blending modes to blend over batched images.
        # blend_rate:               The strength of the blending.

        noise_blend_rate = noise_blend_rate / 2.25

        if blend_mode != "off":
            blended_image = image[0:1]

            for i in range(1, image.size(0)):
                blended_image = blend_images(
                    blended_image, image[i:i+1], blend_mode=blend_mode, blend_rate=blend_rate)

                max_value = torch.max(blended_image)
                blended_image /= max_value

            image = blended_image

        noisy_image = torch.randn_like(image) * noise_intensity
        noisy_image = noisy_image.movedim(-1, 1)

        image = image.movedim(-1, 1)
        image_height, image_width = image.shape[-2:]

        r_mean = torch.mean(image[:, 0, :, :])
        g_mean = torch.mean(image[:, 1, :, :])
        b_mean = torch.mean(image[:, 2, :, :])

        fill_value = (r_mean.item(), g_mean.item(), b_mean.item())

        elastic_transformer = T.ElasticTransform(
            alpha=float(magnitude), sigma=float(smoothness), fill=fill_value)
        transformed_img = elastic_transformer(image)

        if saturation_correction != 1.0:
            transformed_img = F.adjust_saturation(
                transformed_img, saturation_factor=saturation_correction)

        if noise_resize_factor > 0:
            resize_cropper = T.RandomResizedCrop(
                size=(image_height // noise_resize_factor, image_width // noise_resize_factor))

            resized_crop = resize_cropper(noisy_image)

            resized_img = T.Resize(
                size=(image_height, image_width))(resized_crop)
            resized_img = resized_img.movedim(1, -1)
        else:
            resized_img = noisy_image.movedim(1, -1)

        if image.size(0) == 1:
            result = transformed_img.squeeze(0).permute(
                1, 2, 0) + (resized_img * noise_blend_rate)
        else:
            result = transformed_img.squeeze(0).permute(
                [0, 2, 3, 1])[:, :, :, :3] + (resized_img * noise_blend_rate)

        latent = None

        if vae_opt is not None:
            latent = vae_opt.encode(result[:, :, :, :3])

        return (result, {"samples": latent})

# ------------------------------------------------------------------------------------------------------------------ #


class YANCMaskCurves:
    @classmethod
    def INPUT_TYPES(s):
        return {"required":
                {
                    "mask": ("MASK",),
                    "low_value_factor": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 3.0, "step": 0.05, "round": 0.05}),
                    "mid_low_value_factor": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 3.0, "step": 0.05, "round": 0.05}),
                    "mid_value_factor": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 3.0, "step": 0.05, "round": 0.05}),
                    "high_value_factor": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 3.0, "step": 0.05, "round": 0.05}),
                    "brightness": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 3.0, "step": 0.05, "round": 0.05}),
                },
                }

    CATEGORY = yanc_root_name + yanc_sub_masking

    RETURN_TYPES = ("MASK",)
    RETURN_NAMES = ("mask",)
    FUNCTION = "do_it"

    def do_it(self, mask, low_value_factor, mid_low_value_factor, mid_value_factor, high_value_factor, brightness):

        low_mask = (mask < 0.25).float()
        mid_low_mask = ((mask >= 0.25) & (mask < 0.5)).float()
        mid_mask = ((mask >= 0.5) & (mask < 0.75)).float()
        high_mask = (mask >= 0.75).float()

        low_mask = low_mask * (mask * low_value_factor)
        mid_low_mask = mid_low_mask * (mask * mid_low_value_factor)
        mid_mask = mid_mask * (mask * mid_value_factor)
        high_mask = high_mask * (mask * high_value_factor)

        final_mask = low_mask + mid_low_mask + mid_mask + high_mask
        final_mask = final_mask * brightness
        final_mask = torch.clamp(final_mask, 0, 1)

        return (final_mask,)


# ------------------------------------------------------------------------------------------------------------------ #


class YANCLightSourceMask:
    @classmethod
    def INPUT_TYPES(s):
        return {"required":
                {
                    "image": ("IMAGE",),
                    "threshold": ("FLOAT", {"default": 0.33, "min": 0.0, "max": 1.0, "step": 0.01, "round": 0.01}),
                },
                }

    CATEGORY = yanc_root_name + yanc_sub_masking

    RETURN_TYPES = ("MASK",)
    RETURN_NAMES = ("mask",)
    FUNCTION = "do_it"

    def do_it(self, image, threshold):
        batch_size, height, width, _ = image.shape

        kernel_size = max(33, int(0.05 * min(height, width)))
        kernel_size = kernel_size if kernel_size % 2 == 1 else kernel_size + 1
        sigma = max(1.0, kernel_size / 5.0)

        masks = []

        for i in range(batch_size):
            mask = image[i].permute(2, 0, 1)
            mask = torch.mean(mask, dim=0)

            mask = torch.where(mask > threshold, mask * 3.0,
                               torch.tensor(0.0, device=mask.device))
            mask.clamp_(min=0.0, max=1.0)

            mask = mask.unsqueeze(0).unsqueeze(0)

            blur = T.GaussianBlur(kernel_size=(
                kernel_size, kernel_size), sigma=(sigma, sigma))
            mask = blur(mask)

            mask = mask.squeeze(0).squeeze(0)
            masks.append(mask)

        masks = torch.stack(masks)

        return (masks,)


# ------------------------------------------------------------------------------------------------------------------ #


class YANCNormalMapLighting:

    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "diffuse_map": ("IMAGE",),
                "normal_map": ("IMAGE",),
                "specular_map": ("IMAGE",),
                "light_yaw": ("FLOAT", {"default": 45, "min": -180, "max": 180, "step": 1}),
                "light_pitch": ("FLOAT", {"default": 30, "min": -90, "max": 90, "step": 1}),
                "specular_power": ("FLOAT", {"default": 32, "min": 1, "max": 200, "step": 1}),
                "ambient_light": ("FLOAT", {"default": 0.50, "min": 0, "max": 1, "step": 0.01}),
                "NormalDiffuseStrength": ("FLOAT", {"default": 1.00, "min": 0, "max": 5.0, "step": 0.01}),
                "SpecularHighlightsStrength": ("FLOAT", {"default": 1.00, "min": 0, "max": 5.0, "step": 0.01}),
                "TotalGain": ("FLOAT", {"default": 1.00, "min": 0, "max": 2.0, "step": 0.01}),
                "color": ("INT", {"default": 0xFFFFFF, "min": 0, "max": 0xFFFFFF, "step": 1, "display": "color"}),
            },
            "optional": {
                "mask": ("MASK",),
            }
        }

    RETURN_TYPES = ("IMAGE",)

    FUNCTION = "do_it"

    CATEGORY = yanc_root_name + yanc_sub_image

    def resize_tensor(self, tensor, size):
        return torch.nn.functional.interpolate(tensor, size=size, mode='bilinear', align_corners=False)

    def do_it(self, diffuse_map, normal_map, specular_map, light_yaw, light_pitch, specular_power, ambient_light, NormalDiffuseStrength, SpecularHighlightsStrength, TotalGain, color, mask=None,):
        if mask is None:
            mask = torch.ones_like(diffuse_map[:, :, :, 0])

        diffuse_tensor = diffuse_map.permute(
            0, 3, 1, 2)
        normal_tensor = normal_map.permute(
            0, 3, 1, 2) * 2.0 - 1.0
        specular_tensor = specular_map.permute(
            0, 3, 1, 2)
        mask_tensor = mask.unsqueeze(1)
        mask_tensor = mask_tensor.expand(-1, 3, -1, -1)

        target_size = (diffuse_tensor.shape[2], diffuse_tensor.shape[3])
        normal_tensor = self.resize_tensor(normal_tensor, target_size)
        specular_tensor = self.resize_tensor(specular_tensor, target_size)
        mask_tensor = self.resize_tensor(mask_tensor, target_size)

        normal_tensor = torch.nn.functional.normalize(normal_tensor, dim=1)

        light_direction = self.euler_to_vector(light_yaw, light_pitch, 0)
        light_direction = light_direction.view(1, 3, 1, 1)

        camera_direction = self.euler_to_vector(0, 0, 0)
        camera_direction = camera_direction.view(1, 3, 1, 1)

        light_color = self.int_to_rgb(color)
        light_color_tensor = torch.tensor(
            light_color).view(1, 3, 1, 1)

        diffuse = torch.sum(normal_tensor * light_direction,
                            dim=1, keepdim=True)
        diffuse = torch.clamp(diffuse, 0, 1)
        diffuse = diffuse * light_color_tensor

        half_vector = torch.nn.functional.normalize(
            light_direction + camera_direction, dim=1)
        specular = torch.sum(normal_tensor * half_vector, dim=1, keepdim=True)
        specular = torch.pow(torch.clamp(specular, 0, 1), specular_power)

        specular = specular * light_color_tensor

        if diffuse.shape != target_size:
            diffuse = self.resize_tensor(diffuse, target_size)
        if specular.shape != target_size:
            specular = self.resize_tensor(specular, target_size)

        output_tensor = (diffuse_tensor * (ambient_light + diffuse * NormalDiffuseStrength) +
                         specular_tensor * specular * SpecularHighlightsStrength) * TotalGain

        output_tensor = output_tensor * mask_tensor + \
            diffuse_tensor * (1 - mask_tensor)

        output_tensor = output_tensor.permute(
            0, 2, 3, 1)

        return (output_tensor,)

    def euler_to_vector(self, yaw, pitch, roll):
        yaw_rad = np.radians(yaw)
        pitch_rad = np.radians(pitch)
        roll_rad = np.radians(roll)

        cos_pitch = np.cos(pitch_rad)
        sin_pitch = np.sin(pitch_rad)
        cos_yaw = np.cos(yaw_rad)
        sin_yaw = np.sin(yaw_rad)

        direction = np.array([
            sin_yaw * cos_pitch,
            sin_pitch,
            cos_pitch * cos_yaw
        ])

        return torch.from_numpy(direction).float()

    def int_to_rgb(self, color_int):
        r = (color_int >> 16) & 0xFF
        g = (color_int >> 8) & 0xFF
        b = color_int & 0xFF

        return (r / 255.0, g / 255.0, b / 255.0)


# ------------------------------------------------------------------------------------------------------------------ #


class YANCRGBColor:
    @classmethod
    def INPUT_TYPES(s):
        return {"required":
                {
                    "red": ("INT", {"default": 0, "min": 0, "max": 255, "step": 1}),
                    "green": ("INT", {"default": 0, "min": 0, "max": 255, "step": 1}),
                    "blue": ("INT", {"default": 0, "min": 0, "max": 255, "step": 1}),
                    "plus_minus": ("INT", {"default": 0, "min": -255, "max": 255, "step": 1}),
                },
                }

    CATEGORY = yanc_root_name + yanc_sub_utils

    RETURN_TYPES = ("INT", "INT", "INT", "INT", "STRING",)
    RETURN_NAMES = ("int", "red", "green", "blue", "hex",)
    FUNCTION = "do_it"

    def do_it(self, red, green, blue, plus_minus):
        total = red + green + blue

        r_ratio = red / total if total != 0 else 0
        g_ratio = green / total if total != 0 else 0
        b_ratio = blue / total if total != 0 else 0

        if plus_minus > 0:
            max_plus_minus = min((255 - red) / r_ratio if r_ratio > 0 else float('inf'),
                                (255 - green) / g_ratio if g_ratio > 0 else float('inf'),
                                (255 - blue) / b_ratio if b_ratio > 0 else float('inf'))
            effective_plus_minus = min(plus_minus, max_plus_minus)
        else:
            max_plus_minus = min(red / r_ratio if r_ratio > 0 else float('inf'),
                                green / g_ratio if g_ratio > 0 else float('inf'),
                                blue / b_ratio if b_ratio > 0 else float('inf'))
            effective_plus_minus = max(plus_minus, -max_plus_minus)

        new_r = red + effective_plus_minus * r_ratio
        new_g = green + effective_plus_minus * g_ratio
        new_b = blue + effective_plus_minus * b_ratio

        new_r = max(0, min(255, round(new_r)))
        new_g = max(0, min(255, round(new_g)))
        new_b = max(0, min(255, round(new_b)))

        color = (new_r << 16) | (new_g << 8) | new_b

        hex_color = "#{:02x}{:02x}{:02x}".format(
            int(new_r), int(new_g), int(new_b)).upper()

        return (color, new_r, new_g, new_b, hex_color)


# ------------------------------------------------------------------------------------------------------------------ #


class YANCGetMeanColor:
    @classmethod
    def INPUT_TYPES(s):
        return {"required":
                {
                    "image": ("IMAGE",),
                    "amplify": ("BOOLEAN", {"default": False})
                },
                "optional":
                {
                    "mask_opt": ("MASK",),
                },
                }

    CATEGORY = yanc_root_name + yanc_sub_utils

    RETURN_TYPES = ("INT", "INT", "INT", "INT", "STRING")
    RETURN_NAMES = ("int", "red", "green", "blue", "hex")
    FUNCTION = "do_it"

    def do_it(self, image, amplify, mask_opt=None):
        masked_image = image.clone()

        if mask_opt is not None:
            if mask_opt.shape[1:3] != image.shape[1:3]:
                raise ValueError(
                    "Mask and image spatial dimensions must match.")

            mask_opt = mask_opt.unsqueeze(-1)
            masked_image = masked_image * mask_opt

            num_masked_pixels = torch.sum(mask_opt)
            if num_masked_pixels == 0:
                raise ValueError(
                    "No masked pixels found in the image. Please set a mask.")

            sum_r = torch.sum(masked_image[:, :, :, 0])
            sum_g = torch.sum(masked_image[:, :, :, 1])
            sum_b = torch.sum(masked_image[:, :, :, 2])

            r_mean = sum_r / num_masked_pixels
            g_mean = sum_g / num_masked_pixels
            b_mean = sum_b / num_masked_pixels
        else:
            r_mean = torch.mean(masked_image[:, :, :, 0])
            g_mean = torch.mean(masked_image[:, :, :, 1])
            b_mean = torch.mean(masked_image[:, :, :, 2])

        r_mean_255 = r_mean.item() * 255.0
        g_mean_255 = g_mean.item() * 255.0
        b_mean_255 = b_mean.item() * 255.0

        if amplify:
            highest_value = max(r_mean_255, g_mean_255, b_mean_255)
            diff_to_max = 255.0 - highest_value

            amp_factor = 1.0

            r_mean_255 += diff_to_max * amp_factor * \
                (r_mean_255 / highest_value)
            g_mean_255 += diff_to_max * amp_factor * \
                (g_mean_255 / highest_value)
            b_mean_255 += diff_to_max * amp_factor * \
                (b_mean_255 / highest_value)

            r_mean_255 = min(max(r_mean_255, 0), 255)
            g_mean_255 = min(max(g_mean_255, 0), 255)
            b_mean_255 = min(max(b_mean_255, 0), 255)

        fill_value = (int(r_mean_255) << 16) + \
            (int(g_mean_255) << 8) + int(b_mean_255)

        hex_color = "#{:02x}{:02x}{:02x}".format(
            int(r_mean_255), int(g_mean_255), int(b_mean_255)).upper()

        return (fill_value, int(r_mean_255), int(g_mean_255), int(b_mean_255), hex_color,)


# ------------------------------------------------------------------------------------------------------------------ #
NODE_CLASS_MAPPINGS = {
    # Image
    "> Rotate Image": YANCRotateImage,
    "> Scale Image to Side": YANCScaleImageToSide,
    "> Resolution by Aspect Ratio": YANCResolutionByAspectRatio,
    "> Load Image": YANCLoadImageAndFilename,
    "> Save Image": YANCSaveImage,
    "> Load Image From Folder": YANCLoadImageFromFolder,
    "> Normal Map Lighting": YANCNormalMapLighting,

    # Text
    "> Text": YANCText,
    "> Text Combine": YANCTextCombine,
    "> Text Pick Random Line": YANCTextPickRandomLine,
    "> Clear Text": YANCClearText,
    "> Text Replace": YANCTextReplace,
    "> Text Random Weights": YANCTextRandomWeights,

    # Basics
    "> Int to Text": YANCIntToText,
    "> Int": YANCInt,
    "> Float to Int": YANCFloatToInt,

    # Noise Injection Sampler
    "> NIKSampler": YANCNIKSampler,
    "> Noise From Image": YANCNoiseFromImage,

    # Masking
    "> Mask Curves": YANCMaskCurves,
    "> Light Source Mask": YANCLightSourceMask,

    # Utils
    "> Get Mean Color": YANCGetMeanColor,
    "> RGB Color": YANCRGBColor,
}

# A dictionary that contains the friendly/humanly readable titles for the nodes
NODE_DISPLAY_NAME_MAPPINGS = {
    # Image
    "> Rotate Image": "😼> Rotate Image",
    "> Scale Image to Side": "😼> Scale Image to Side",
    "> Resolution by Aspect Ratio": "😼> Resolution by Aspect Ratio",
    "> Load Image": "😼> Load Image",
    "> Save Image": "😼> Save Image",
    "> Load Image From Folder": "😼> Load Image From Folder",
    "> Normal Map Lighting": "😼> Normal Map Lighting",

    # Text
    "> Text": "😼> Text",
    "> Text Combine": "😼> Text Combine",
    "> Text Pick Random Line": "😼> Text Pick Random Line",
    "> Clear Text": "😼> Clear Text",
    "> Text Replace": "😼> Text Replace",
    "> Text Random Weights": "😼> Text Random Weights",

    # Basics
    "> Int to Text": "😼> Int to Text",
    "> Int": "😼> Int",
    "> Float to Int": "😼> Float to Int",

    # Noise Injection Sampler
    "> NIKSampler": "😼> NIKSampler",
    "> Noise From Image": "😼> Noise From Image",

    # Masking
    "> Mask Curves": "😼> Mask Curves",
    "> Light Source Mask": "😼> Light Source Mask",

    # Utils
    "> Get Mean Color": "😼> Get Mean Color",
    "> RGB Color": "😼> RGB Color"
}
