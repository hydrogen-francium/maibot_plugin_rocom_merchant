# 洛克王国远行商人插件 for MaiBot

这是把原 AstrBot 插件 `astrbot_plugin_rocom` 迁到 MaiBot 的版本。

## 安装

1. 把这个目录放进 MaiBot 的 `plugins` 目录。
2. 启动一次 MaiBot，生成 `config.toml`。
3. 在 `config.toml` 里填写 `api.wegame_api_key`。
4. 把自己的 QQ 号填进 `permissions.admin_id_list`。
5. 安装浏览器：

```bash
python -m playwright install chromium
```

6. 重启 MaiBot。

## 说明

- 这个插件没有 `api.wegame_api_key` 就用不了。
- 原插件 README 里给过可用 key，这个仓库不直接写明文凭证，自己去原插件页面看。
- 其他配置项以 [`plugin.py`](maibot_plugin_rocom_merchant\plugin.py) 里的 `config_schema` 为准。

## 命令

- `/远行商人`：查询当前远行商人商品
- `/订阅远行商人`：按默认商品订阅当前群播报，仅管理员可用
- `/订阅远行商人 国王球 棱镜球`：按指定商品订阅当前群播报，仅管理员可用
- `/取消订阅远行商人`：取消当前群订阅，仅管理员可用
- `/远行商人播报 开`：临时打开远行商人播报，仅管理员可用
- `/远行商人播报 关`：临时关闭远行商人播报，仅管理员可用
- `/远行商人播报 状态`：查看当前播报开关状态，仅管理员可用
- `/远行商人重写 开`：临时打开 LLM 重写播报，仅管理员可用
- `/远行商人重写 关`：临时关闭 LLM 重写播报，仅管理员可用
- `/远行商人重写 状态`：查看当前 LLM 重写开关状态，仅管理员可用
- `/远行商人订阅列表`：查看全部群订阅情况，仅管理员可用
- `/洛克查蛋 精灵名`：按精灵名查询蛋组和相关信息
- `/洛克查蛋 25`：按身高反查精灵
- `/洛克查蛋 25 1.5`：按身高和体重反查精灵
- `/洛克查蛋 身高25 体重1.5`：按带关键字的写法反查精灵
- `/洛克配种 父体 母体`：判断两个精灵能不能配种，默认前父后母
- `/洛克配种 精灵名`：查询想孵这个精灵时可用的配种方案

## 鸣谢

原插件：

https://github.com/Entropy-Increase-Team/astrbot_plugin_rocom
