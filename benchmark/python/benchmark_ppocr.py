# Copyright (c) 2022 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import fastdeploy as fd
import cv2
import os
import numpy as np
import time


def parse_arguments():
    import argparse
    import ast
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model_dir", required=True, help="Model dir of PPOCR.")
    parser.add_argument(
        "--det_model", required=True, help="Path of Detection model of PPOCR.")
    parser.add_argument(
        "--cls_model",
        required=True,
        help="Path of Classification model of PPOCR.")
    parser.add_argument(
        "--rec_model",
        required=True,
        help="Path of Recognization model of PPOCR.")
    parser.add_argument(
        "--rec_label_file",
        required=True,
        help="Path of Recognization label file of PPOCR.")
    parser.add_argument(
        "--image", type=str, required=False, help="Path of test image file.")
    parser.add_argument(
        "--cpu_num_thread",
        type=int,
        default=8,
        help="default number of cpu thread.")
    parser.add_argument(
        "--device_id", type=int, default=0, help="device(gpu) id")
    parser.add_argument(
        "--iter_num",
        required=True,
        type=int,
        default=300,
        help="number of iterations for computing performance.")
    parser.add_argument(
        "--device",
        default="cpu",
        help="Type of inference device, support 'cpu' or 'gpu'.")
    parser.add_argument(
        "--backend",
        type=str,
        default="default",
        help="inference backend, default, ort, ov, trt, paddle, paddle_trt.")
    parser.add_argument(
        "--enable_trt_fp16",
        type=ast.literal_eval,
        default=False,
        help="whether enable fp16 in trt backend")
    parser.add_argument(
        "--enable_collect_memory_info",
        type=ast.literal_eval,
        default=False,
        help="whether enable collect memory info")
    args = parser.parse_args()
    return args


def build_option(args):
    option = fd.RuntimeOption()
    device = args.device
    backend = args.backend
    enable_trt_fp16 = args.enable_trt_fp16
    option.set_cpu_thread_num(args.cpu_num_thread)
    if device == "gpu":
        option.use_gpu()
        if backend == "ort":
            option.use_ort_backend()
        elif backend == "paddle":
            option.use_paddle_backend()
        elif backend in ["trt", "paddle_trt"]:
            option.use_trt_backend()
            if backend == "paddle_trt":
                option.paddle_infer_option.collect_trt_shape = True
                option.use_paddle_infer_backend()
                option.paddle_infer_option.enable_trt = True
            if enable_trt_fp16:
                option.enable_trt_fp16()
        elif backend == "default":
            return option
        else:
            raise Exception(
                "While inference with GPU, only support default/ort/paddle/trt/paddle_trt now, {} is not supported.".
                format(backend))
    elif device == "cpu":
        if backend == "ort":
            option.use_ort_backend()
        elif backend == "ov":
            option.use_openvino_backend()
        elif backend == "paddle":
            option.use_paddle_backend()
        elif backend == "default":
            return option
        else:
            raise Exception(
                "While inference with CPU, only support default/ort/ov/paddle now, {} is not supported.".
                format(backend))
    else:
        raise Exception(
            "Only support device CPU/GPU now, {} is not supported.".format(
                device))

    return option


class StatBase(object):
    """StatBase"""
    nvidia_smi_path = "nvidia-smi"
    gpu_keys = ('index', 'uuid', 'name', 'timestamp', 'memory.total',
                'memory.free', 'memory.used', 'utilization.gpu',
                'utilization.memory')
    nu_opt = ',nounits'
    cpu_keys = ('cpu.util', 'memory.util', 'memory.used')


class Monitor(StatBase):
    """Monitor"""

    def __init__(self, use_gpu=False, gpu_id=0, interval=0.1):
        self.result = {}
        self.gpu_id = gpu_id
        self.use_gpu = use_gpu
        self.interval = interval
        self.cpu_stat_q = multiprocessing.Queue()

    def start(self):
        cmd = '%s --id=%s --query-gpu=%s --format=csv,noheader%s -lms 50' % (
            StatBase.nvidia_smi_path, self.gpu_id, ','.join(StatBase.gpu_keys),
            StatBase.nu_opt)
        if self.use_gpu:
            self.gpu_stat_worker = subprocess.Popen(
                cmd,
                stderr=subprocess.STDOUT,
                stdout=subprocess.PIPE,
                shell=True,
                close_fds=True,
                preexec_fn=os.setsid)
        # cpu stat
        pid = os.getpid()
        self.cpu_stat_worker = multiprocessing.Process(
            target=self.cpu_stat_func,
            args=(self.cpu_stat_q, pid, self.interval))
        self.cpu_stat_worker.start()

    def stop(self):
        try:
            if self.use_gpu:
                os.killpg(self.gpu_stat_worker.pid, signal.SIGUSR1)
            # os.killpg(p.pid, signal.SIGTERM)
            self.cpu_stat_worker.terminate()
            self.cpu_stat_worker.join(timeout=0.01)
        except Exception as e:
            print(e)
            return

        # gpu
        if self.use_gpu:
            lines = self.gpu_stat_worker.stdout.readlines()
            lines = [
                line.strip().decode("utf-8") for line in lines
                if line.strip() != ''
            ]
            gpu_info_list = [{
                k: v
                for k, v in zip(StatBase.gpu_keys, line.split(', '))
            } for line in lines]
            if len(gpu_info_list) == 0:
                return
            result = gpu_info_list[0]
            for item in gpu_info_list:
                for k in item.keys():
                    if k not in ["name", "uuid", "timestamp"]:
                        result[k] = max(int(result[k]), int(item[k]))
                    else:
                        result[k] = max(result[k], item[k])
            self.result['gpu'] = result

        # cpu
        cpu_result = {}
        if self.cpu_stat_q.qsize() > 0:
            cpu_result = {
                k: v
                for k, v in zip(StatBase.cpu_keys, self.cpu_stat_q.get())
            }
        while not self.cpu_stat_q.empty():
            item = {
                k: v
                for k, v in zip(StatBase.cpu_keys, self.cpu_stat_q.get())
            }
            for k in StatBase.cpu_keys:
                cpu_result[k] = max(cpu_result[k], item[k])
        cpu_result['name'] = cpuinfo.get_cpu_info()['brand_raw']
        self.result['cpu'] = cpu_result

    def output(self):
        return self.result

    def cpu_stat_func(self, q, pid, interval=0.0):
        """cpu stat function"""
        stat_info = psutil.Process(pid)
        while True:
            # pid = os.getpid()
            cpu_util, mem_util, mem_use = stat_info.cpu_percent(
            ), stat_info.memory_percent(), round(stat_info.memory_info().rss /
                                                 1024.0 / 1024.0, 4)
            q.put([cpu_util, mem_util, mem_use])
            time.sleep(interval)
        return


if __name__ == '__main__':

    args = parse_arguments()
    option = build_option(args)
    # Detection Model
    det_model_file = os.path.join(args.model_dir, args.det_model,
                                  "inference.pdmodel")
    det_params_file = os.path.join(args.model_dir, args.det_model,
                                   "inference.pdiparams")
    # Classification Model
    cls_model_file = os.path.join(args.model_dir, args.cls_model,
                                  "inference.pdmodel")
    cls_params_file = os.path.join(args.model_dir, args.cls_model,
                                   "inference.pdiparams")
    # Recognition Model
    rec_model_file = os.path.join(args.model_dir, args.rec_model,
                                  "inference.pdmodel")
    rec_params_file = os.path.join(args.model_dir, args.rec_model,
                                   "inference.pdiparams")
    rec_label_file = os.path.join(args.model_dir, args.rec_label_file)

    gpu_id = args.device_id
    enable_collect_memory_info = args.enable_collect_memory_info
    dump_result = dict()
    end2end_statis = list()
    cpu_mem = list()
    gpu_mem = list()
    gpu_util = list()
    if args.device == "cpu":
        file_path = args.model_dir + "_model_" + args.backend + "_" + \
            args.device + "_" + str(args.cpu_num_thread) + ".txt"
    else:
        if args.enable_trt_fp16:
            file_path = args.model_dir + "_model_" + args.backend + "_fp16_" + args.device + ".txt"
        else:
            file_path = args.model_dir + "_model_" + args.backend + "_" + args.device + ".txt"
    f = open(file_path, "w")
    f.writelines("===={}====: \n".format(os.path.split(file_path)[-1][:-4]))

    try:
        if "OCRv2" in args.model_dir:
            det_option = option
            if args.backend in ["trt", "paddle_trt"]:
                det_option.trt_option.set_shape(
                    "x", [1, 3, 64, 64], [1, 3, 640, 640], [1, 3, 960, 960])
            det_model = fd.vision.ocr.DBDetector(
                det_model_file, det_params_file, runtime_option=det_option)
            cls_option = option
            if args.backend in ["trt", "paddle_trt"]:
                cls_option.trt_option.set_shape(
                    "x", [1, 3, 48, 10], [10, 3, 48, 320], [64, 3, 48, 1024])
            cls_model = fd.vision.ocr.Classifier(
                cls_model_file, cls_params_file, runtime_option=cls_option)
            rec_option = option
            if args.backend in ["trt", "paddle_trt"]:
                rec_option.trt_option.set_shape(
                    "x", [1, 3, 32, 10], [10, 3, 32, 320], [32, 3, 32, 2304])
            rec_model = fd.vision.ocr.Recognizer(
                rec_model_file,
                rec_params_file,
                rec_label_file,
                runtime_option=rec_option)
            model = fd.vision.ocr.PPOCRv2(
                det_model=det_model, cls_model=cls_model, rec_model=rec_model)
        elif "OCRv3" in args.model_dir:
            det_option = option
            if args.backend in ["trt", "paddle_trt"]:
                det_option.trt_option.set_shape(
                    "x", [1, 3, 64, 64], [1, 3, 640, 640], [1, 3, 960, 960])
            det_model = fd.vision.ocr.DBDetector(
                det_model_file, det_params_file, runtime_option=det_option)
            cls_option = option
            if args.backend in ["trt", "paddle_trt"]:
                cls_option.trt_option.set_shape(
                    "x", [1, 3, 48, 10], [10, 3, 48, 320], [64, 3, 48, 1024])
            cls_model = fd.vision.ocr.Classifier(
                cls_model_file, cls_params_file, runtime_option=cls_option)
            rec_option = option
            if args.backend in ["trt", "paddle_trt"]:
                rec_option.trt_option.set_shape(
                    "x", [1, 3, 48, 10], [10, 3, 48, 320], [64, 3, 48, 2304])
            rec_model = fd.vision.ocr.Recognizer(
                rec_model_file,
                rec_params_file,
                rec_label_file,
                runtime_option=rec_option)
            model = fd.vision.ocr.PPOCRv3(
                det_model=det_model, cls_model=cls_model, rec_model=rec_model)
        elif "OCRv4" in args.model_dir:
            det_option = option
            if args.backend in ["trt", "paddle_trt"]:
                det_option.trt_option.set_shape(
                    "x", [1, 3, 64, 64], [1, 3, 640, 640], [1, 3, 960, 960])
            det_model = fd.vision.ocr.DBDetector(
                det_model_file, det_params_file, runtime_option=det_option)
            cls_option = option
            if args.backend in ["trt", "paddle_trt"]:
                cls_option.trt_option.set_shape(
                    "x", [1, 3, 48, 10], [10, 3, 48, 320], [64, 3, 48, 1024])
            cls_model = fd.vision.ocr.Classifier(
                cls_model_file, cls_params_file, runtime_option=cls_option)
            rec_option = option
            if args.backend in ["trt", "paddle_trt"]:
                rec_option.trt_option.set_shape(
                    "x", [1, 3, 48, 10], [10, 3, 48, 320], [64, 3, 48, 2304])
            rec_model = fd.vision.ocr.Recognizer(
                rec_model_file,
                rec_params_file,
                rec_label_file,
                runtime_option=rec_option)
            model = fd.vision.ocr.PPOCRv4(
                det_model=det_model, cls_model=cls_model, rec_model=rec_model)
        else:
            raise Exception("model {} not support now in ppocr series".format(
                args.model_dir))
        if enable_collect_memory_info:
            import multiprocessing
            import subprocess
            import psutil
            import signal
            import cpuinfo
            enable_gpu = args.device == "gpu"
            monitor = Monitor(enable_gpu, gpu_id)
            monitor.start()

        det_model.enable_record_time_of_runtime()
        cls_model.enable_record_time_of_runtime()
        rec_model.enable_record_time_of_runtime()
        im_ori = cv2.imread(args.image)
        for i in range(args.iter_num):
            im = im_ori
            start = time.time()
            result = model.predict(im)
            end2end_statis.append(time.time() - start)

        runtime_statis_det = det_model.print_statis_info_of_runtime()
        runtime_statis_cls = cls_model.print_statis_info_of_runtime()
        runtime_statis_rec = rec_model.print_statis_info_of_runtime()

        warmup_iter = args.iter_num // 5
        end2end_statis_repeat = end2end_statis[warmup_iter:]
        if enable_collect_memory_info:
            monitor.stop()
            mem_info = monitor.output()
            dump_result["cpu_rss_mb"] = mem_info['cpu'][
                'memory.used'] if 'cpu' in mem_info else 0
            dump_result["gpu_rss_mb"] = mem_info['gpu'][
                'memory.used'] if 'gpu' in mem_info else 0
            dump_result["gpu_util"] = mem_info['gpu'][
                'utilization.gpu'] if 'gpu' in mem_info else 0

        dump_result["runtime"] = (
            runtime_statis_det["avg_time"] + runtime_statis_cls["avg_time"] +
            runtime_statis_rec["avg_time"]) * 1000
        dump_result["end2end"] = np.mean(end2end_statis_repeat) * 1000

        f.writelines("Runtime(ms): {} \n".format(str(dump_result["runtime"])))
        f.writelines("End2End(ms): {} \n".format(str(dump_result["end2end"])))
        print("Runtime(ms): {} \n".format(str(dump_result["runtime"])))
        print("End2End(ms): {} \n".format(str(dump_result["end2end"])))
        if enable_collect_memory_info:
            f.writelines("cpu_rss_mb: {} \n".format(
                str(dump_result["cpu_rss_mb"])))
            f.writelines("gpu_rss_mb: {} \n".format(
                str(dump_result["gpu_rss_mb"])))
            f.writelines("gpu_util: {} \n".format(
                str(dump_result["gpu_util"])))
            print("cpu_rss_mb: {} \n".format(str(dump_result["cpu_rss_mb"])))
            print("gpu_rss_mb: {} \n".format(str(dump_result["gpu_rss_mb"])))
            print("gpu_util: {} \n".format(str(dump_result["gpu_util"])))
    except:
        f.writelines("!!!!!Infer Failed\n")

    f.close()
