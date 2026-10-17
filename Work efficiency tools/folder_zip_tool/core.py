# -*- coding: utf-8 -*-
"""フォルダ化・zip化のコア処理(Windows非依存)。"""
import datetime
import os
import re
import shutil
import zipfile

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_name(name):
    name = _INVALID.sub("_", name or "").strip().rstrip(". ")
    return name or "新しいフォルダ"


def default_name(paths):
    """選択内容から既定の名前を決める。"""
    if len(paths) == 1:
        p = paths[0].rstrip("\\/")
        base = os.path.basename(p)
        stem = base if os.path.isdir(p) else os.path.splitext(base)[0]
        return sanitize_name(stem)
    return "まとめ_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def unique_path(path, is_file=False):
    """既に存在する場合は ' (2)' のように連番を付けた未使用パスを返す。"""
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path) if is_file else (path, "")
    n = 2
    while True:
        cand = "%s (%d)%s" % (root, n, ext)
        if not os.path.exists(cand):
            return cand
        n += 1


def _parent_of(paths):
    return os.path.dirname(os.path.abspath(paths[0].rstrip("\\/")))


def move_to_folder(paths, name):
    """新しいフォルダを作り、選択項目をそこへ『移動』する。作成したフォルダを返す。"""
    dest = unique_path(os.path.join(_parent_of(paths), sanitize_name(name)))
    os.makedirs(dest)
    for p in paths:
        p = p.rstrip("\\/")
        target = unique_path(
            os.path.join(dest, os.path.basename(p)), is_file=os.path.isfile(p)
        )
        shutil.move(p, target)
    return dest


def _copy_into(paths, dest_dir):
    for p in paths:
        p = p.rstrip("\\/")
        target = unique_path(
            os.path.join(dest_dir, os.path.basename(p)), is_file=os.path.isfile(p)
        )
        if os.path.isdir(p):
            shutil.copytree(p, target)
        else:
            shutil.copy2(p, target)


def _add_tree(zf, root_dir, prefix):
    zf.write(root_dir, prefix + "/")
    for dirpath, dirnames, filenames in os.walk(root_dir):
        rel = os.path.relpath(dirpath, root_dir)
        base = prefix if rel == "." else prefix + "/" + rel.replace(os.sep, "/")
        for d in dirnames:
            zf.write(os.path.join(dirpath, d), base + "/" + d + "/")
        for f in filenames:
            zf.write(os.path.join(dirpath, f), base + "/" + f)


def zip_items(paths, name, keep_folder):
    """選択項目を『コピー』してzip化する。(zipパス, 残したフォルダ or None) を返す。

    zipの中身は「名前/」フォルダの下にまとまる(解凍時に散らばらないため)。
    keep_folder=True なら名前のフォルダも実体として残す。
    """
    parent = _parent_of(paths)
    name = sanitize_name(name)
    zip_path = unique_path(os.path.join(parent, name + ".zip"), is_file=True)
    folder = None
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            if keep_folder:
                folder = unique_path(os.path.join(parent, name))
                os.makedirs(folder)
                _copy_into(paths, folder)
                _add_tree(zf, folder, os.path.basename(folder))
            else:
                zf.writestr(name + "/", b"")
                for p in paths:
                    p = p.rstrip("\\/")
                    arc = name + "/" + os.path.basename(p)
                    if os.path.isdir(p):
                        _add_tree(zf, p, arc)
                    else:
                        zf.write(p, arc)
    except Exception:
        if os.path.exists(zip_path):
            os.remove(zip_path)
        raise
    return zip_path, folder
