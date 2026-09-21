# vxs

Sileo / Zebra / Cydia 越狱软件源，托管在 GitHub Pages。

**源地址**

```
https://fatekingcsr.github.io/vxs/
```

> 地址必须以 `/` 结尾；必须是 HTTPS。GitHub Pages 自带 HTTPS。

## 添加 deb 包

1. 把 `.deb` 放进 `debs/` 目录
2. 运行索引生成器：

   ```bash
   python tools/gen_repo.py
   ```

3. 提交并推送：

   ```bash
   git add debs/ Packages Packages.gz Packages.bz2 Release packages.json repo-data.js
   git commit -m "add: <包名>"
   git push
   ```

推送到 `main` 后，GitHub Actions 也会自动重建索引（改 `debs/`、`repo.json`、
`tools/gen_repo.py` 时触发），本地跑一次只是为了即时看到效果。

## 架构

| 越狱方式 | deb 的 `Architecture` | Theos 配置 |
| --- | --- | --- |
| Dopamine / palera1n **rootless** | `iphoneos-arm64` | `THEOS_PACKAGE_SCHEME = rootless` |
| 传统 rootful | `iphoneos-arm` | 默认 |

`Release` 的 `Architectures` 会自动合并 `debs/` 里所有包的架构，
并兜底补上 `iphoneos-arm64 iphoneos-arm`，两种设备都能看到包。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `Packages` / `.gz` / `.bz2` | 包索引，Sileo 优先读 `.bz2` |
| `Release` | 源元信息 + 索引校验和 |
| `packages.json` | 落地页数据 |
| `repo-data.js` | 同上，用 `<script>` 加载（`file://` 下也能用） |
| `repo.json` | 源名称 / 简介 / 维护者 / 地址，改完重新生成 |
| `CydiaIcon.png` | Sileo 里显示的源图标 |
| `tools/gen_repo.py` | 索引生成器（纯 Python 解 deb，无需 dpkg-deb） |
| `.nojekyll` | 必须保留，否则 GitHub Pages 的 Jekyll 会干扰 |

## 改了源名称 / 简介

编辑 `repo.json`，再跑一次 `python tools/gen_repo.py`。

## 注意

- `.gitignore` 里**显式保留**了 `Packages` / `Release` / `packages.json` /
  `repo-data.js`（用 `!` 反选）。很多 APT 源模板会默认忽略它们，
  导致仓库里索引是 0 字节、Sileo 加源必然失败。
- 所有索引与文本文件必须保持 **LF** 换行（见 `.gitattributes`）。
  CRLF 会改写字节，导致 `Release` 里的校验和对不上、安装报 hash mismatch。
- `tools/gen_repo.py` 里 gzip 用了 `mtime=0`，保证重复生成字节一致，
  避免 Actions 无限提交。
