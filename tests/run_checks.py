"""Run offline acceptance checks and write a candid delivery report."""

import hashlib
import io
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py")
output = io.StringIO()
result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
app = yaml.safe_load((ROOT / "系统异常助手_优化版.yml").read_text())
report = f"""系统异常助手优化版 · 测试报告
日期：2026-10-19

结论：{'本地检查全部通过' if result.wasSuccessful() else '存在失败，不能交付为已通过'}。
执行测试 {result.testsRun} 项，失败 {len(result.failures)} 项，错误 {len(result.errors)} 项。
配置包含 {len(app['workflow']['graph']['nodes'])} 个节点、{len(app['workflow']['graph']['edges'])} 条连线。

验证方式
使用优化版 YAML 内嵌的实际代码节点、变量引用、条件分支、赋值与迭代配置执行模拟对话。
语言模型、知识库检索、图片读取/上传和工单接口使用可控模拟输出，不访问生产服务。
检查所有节点可达、无环、变量引用有效，以及所有内嵌代码可以编译；检索成功出口按 2026-10-19 实际 RunAI 行为修正。
先前模拟器错误地把所有 fail-branch 节点视为支持成功分支，导致未发现检索后终止的问题；现加入该问题的回归检查。
检索故障在此平台由原生错误结束，不再把不存在的友好异常出口当作本地测试通过条件。

覆盖内容
- 命中、部分相关、无关、空结果、改写后命中、查询故障和结构化结果非法。
- 文字提问、模糊问题、只有图片、文字加图片、两轮跟进、致谢、解决和换题。
- 缺少工号、工厂名称歧义、字段补齐、取消、保留草稿继续排查。
- 展示确认前不得提交；修改内容/系统/附件/工号后重新确认；仅“好的”不得提交。
- 同一草稿保持 traceId；不把异常代码当 traceId；已有追踪ID记录拦截重复提交。
- 实际响应标记驱动成功/失败；未知返回或网络异常标为待核实；禁止自动重试。
- 跨轮截图引用、成功附件复用、上传失败重试、过期链接替换、主动不附截图。
- 原文件哈希未变化，知识库与 Top K 保留，提交节点移除自主 Agent。
- 字符串布尔值回归、自然建单说法、否定及引用不误触发、非法补充只问缺项、未识别修改使旧确认失效。
- 原始业务错误、工具异常类型与消息、超时、空响应、映射缺失、重复确认保留上次错误、凭据隐藏与纯文本展示。

真实网页验证（独立于以上本地模拟测试）
- 已将网页修复版导入用户指定 RunAI 应用草稿，未发布。
- 真实未命中样本经过两次检索后返回明确提示，真实命中样本经过一次检索后返回建议及来源引用。
- 命中样本的模型措辞存在超出资料的推断，回答通路通过不代表内容完全忠实；详情和截图见“网页联调记录.txt”。
- 后续已在网页修复三个工单引导节点：真实建单请求返回缺项清单，补充工厂后只追问所属系统；两项齐全后因预览未传工号而保留草稿，没有创建真实工单。
- 错误反馈节点已在网页以标注“模拟验证”的响应运行，普通接口返回和工具异常信息都进入 reply；单节点测试没有调用工单接口。

未验证内容（不能据此声明生产已就绪）
- 实际 Dify 服务器版本号仍未知；已访问 RunAI 页面，真实联调记录另见“网页联调记录.txt”。
- 模型在更多场景下的意图理解和结构化输出遵循度、整体召回质量、答案忠实度及流式首字延迟；目前仅验证少量真实查询及连续工单字段补充。
- 用户两个自定义工具的返回结构、凭据和可用性；没有运行真实上传或创建工单。
- Dify HTTP 节点对你们文件域名的可达性、实际签名有效期、宿主图片大小限制。
- 并发和跨会话幂等；需要业务网关串行化及后端接口提供保障。

接入状态
真实接口映射暂未核实，环境变量 itsm_response_contract 的验证标记默认关闭。
用户可查询知识库、填写及确认草稿；身份和字段校验通过后会调用一次创建接口。
返回映射未核实时如实展示原始返回并标记待核实，不判成功、不自动重试；需真实响应样本完善成功/失败识别。
有截图的工单还需配置上传响应；相对图片地址需填写 dify_files_base_url。

原配置 SHA-256
{hashlib.sha256((ROOT / '系统异常助手.yml').read_bytes()).hexdigest()}

优化版 SHA-256
{hashlib.sha256((ROOT / '系统异常助手_优化版.yml').read_bytes()).hexdigest()}

复现方法
python3 workflow_src/build.py
python3 tests/run_checks.py

详细结果
{output.getvalue()}"""
(ROOT / "测试报告.txt").write_text(report)
print(output.getvalue())
print("测试报告已更新。")
raise SystemExit(0 if result.wasSuccessful() else 1)
