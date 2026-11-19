<!-- auto-mas:i18n
{
  "display_name_i18n": {
    "zh_CN": "MuMu 模拟器适配",
    "en": "MuMu Emulator Adapter",
    "ja": "MuMu エミュレーター"
  },
  "description_i18n": {
    "zh_CN": "MuMu 模拟器适配",
    "en": "MuMu emulator lifecycle and settings",
    "ja": "MuMu エミュレーター管理"
  },
  "tags_i18n": {
    "zh_CN": [
      "模拟器"
    ],
    "en": [
      "emulator"
    ],
    "ja": [
      "エミュレーター"
    ]
  }
}
-->

# MuMu 模拟器适配

通过 AUTO-MAS 游戏管理发现或手动添加安装，选择设备后即可托管。支持设置、稳定模式和配置恢复；恢复失败记录后留待下次尝试。

## 安装与发布

发行包为 `auto-mas-mumu-adapter`，插件 ID 为 `mumu_adapter`。
适用于 Python 3.12 和已接入游戏适配插件的 AUTO-MAS 宿主，要求 `auto-mas-core>=5.9.0b1,<6`。
通过插件市场安装时由宿主提供 core，独立安装时需要提供兼容的 SDK wheel。

首次发布前，在 [PyPI 账户的 Publishing 页面](https://pypi.org/manage/account/publishing/) 登记待定发布者。
项目名填 `auto-mas-mumu-adapter`，仓库所有者填 `AUTO-MAS-Project`，仓库名填 `AUTO-MAS-MuMu-Adapter`，工作流文件名填 `publish.yml`，环境名填 `pypi`。

分支和 PR 会构建并校验 wheel 与源码包。发布时，在默认分支的提交上打与 `pyproject.toml` 版本一致的 `v` 标签并推送；GitHub Actions 通过 Trusted Publishing 上传分发包。
