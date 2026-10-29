# 系统异常助手维护约定

- `系统异常助手_latest.yml` 是当前配置的唯一维护和导入入口。后续更新直接修改这个文件，不再新增按功能或日期命名的交付版本。
- 其他 `系统异常助手*.yml` 文件和 `backups/` 中的 YAML 保留为历史基线。`workflow_src/build*.py` 是基于这些旧基线的构建或迁移工具，不能直接用其输出覆盖 latest；更新应保留 latest 中现有的模型、知识库、工具、环境变量和流程修复。
- 修改代码节点或提示词时，同步维护 `workflow_src/` 中对应的源实现和 latest 中的内嵌内容。当前版本的校验应读取 latest；历史回归测试可以继续使用对应的历史基线。
- 修改后运行 `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_*.py'`。截图、真实接口响应和联调记录按现有忽略规则保留在本地。
