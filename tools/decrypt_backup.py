# -*- coding: utf-8 -*-
"""把「密檔備份」（.enc）還原成可以直接用的 .db 檔。

跟系統存密檔備份時用的是同一套邏輯（純標準函式庫、SHA-256 當金鑰、XOR 混淆，
不是正式加密），密碼要跟存的時候一樣，錯了密碼不會報錯，只會解出一堆亂碼。

用法：
    python tools/decrypt_backup.py 備份檔.enc 還原後.db
"""
import hashlib
import sys


def xor_obfuscate(data, password):
    key = hashlib.sha256(password.encode("utf-8")).digest()
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def main():
    if len(sys.argv) != 3:
        print("用法：python decrypt_backup.py 備份檔.enc 還原後.db")
        sys.exit(1)
    src, dst = sys.argv[1], sys.argv[2]
    password = input("密碼：")
    with open(src, "rb") as f:
        data = f.read()
    with open(dst, "wb") as f:
        f.write(xor_obfuscate(data, password))
    print(f"已寫入 {dst}——密碼錯了也不會報錯，直接開起來看是不是正常的資料庫確認一下。")


if __name__ == "__main__":
    main()
