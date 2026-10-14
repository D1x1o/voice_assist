import os
import sys

print("Python:", sys.executable)
print("Платформа:", sys.platform)
print()

import nvidia.cublas
import nvidia.cudnn

cublas_bin = os.path.join(nvidia.cublas.__path__[0], "bin")
cudnn_bin = os.path.join(nvidia.cudnn.__path__[0], "bin")

print("Папка cublas:", cublas_bin)
print("Существует:", os.path.isdir(cublas_bin))
if os.path.isdir(cublas_bin):
    print("Файлы:", os.listdir(cublas_bin))
print()

print("Папка cudnn:", cudnn_bin)
print("Существует:", os.path.isdir(cudnn_bin))
if os.path.isdir(cudnn_bin):
    print("Файлы:", os.listdir(cudnn_bin))
print()

os.add_dll_directory(cublas_bin)
os.add_dll_directory(cudnn_bin)

import ctypes

for dll_name in ["cublas64_12.dll", "cublasLt64_12.dll"]:
    try:
        ctypes.WinDLL(dll_name)
        print(f"{dll_name}: ЗАГРУЖЕНА УСПЕШНО")
    except OSError as e:
        print(f"{dll_name}: ОШИБКА -> {e}")
