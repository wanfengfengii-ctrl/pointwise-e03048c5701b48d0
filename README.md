# 澄河台架电压采集

澄河电子试制车间的计划员从白班计划登记测量工单，值班员用录制台架下发或撤单，质检员在设备离线后凭固定样本号复核原码和曝光出处。支持同档并采、按设备确认换档和跨班次领件。运行环境为 Python 3.11 标准库与随 Python 提供的 SQLite。

## 运行

在工程根目录运行完整回归与录制演示。`check_project.py` 由运行平台在物化工程时提供。

```text
python3 -B check_project.py
python3 -B -m unittest discover -s tests -t .
python3 -B -m control.dispatch samples/day.json
```

演示使用临时数据库，演示输出包含实际设备轨迹、固定样本及回执和重开后生成的班次报告。白班 A 已撤单，B 原码 5，增益 1/6 伏每码，偏置 −1/6 伏，得到 2/3 伏。C 原码 4，增益 1/4 伏每码，偏置 −1 伏，得到 0/1 伏。B、C 的曝光出处分别为录制第 1、6 步，A 在低档实际曝光后结束。真实零值是该班最小值。

## 一班的工作

1. 计划员建立班次，班次可以没有工单。初始化只建立表和约束，重复初始化保留已有班次、工单、样本、回执和业务修改。
2. 计划员登记班次、工单号、请求档位、换算版次和增益、偏置。工单号在同班内唯一，跨班可重复。登记序在班次内固定。
3. 值班员推进采样。有物理占用时优先用空位执行最早的同档待采单；占用清空时从全队列首单选档。发出换档命令后，匹配命令号及目标档位的设备回执到达才恢复下发。
4. 值班员可以撤销待采单或业务尚未结束的已启动工单。已启动触发可等待自然终止，也可逐个请求停止。原触发仍可能曝光时继续占用其原档位，停止受理不改变这一点。
5. 台架记录实际曝光档位、录制标识、步和位置。完成通知引用其原触发的曝光步。自然完成和明确停止均可释放对应原触发。位置可以复用，原终态重送保持后继工单及成果原状。
6. 采集用原工单冻结的系数换算。输入须为整数原码，布尔值和其他类型均属非法读数。实际档位和原曝光出处必须匹配。计算为原码 × 增益 + 偏置，结果以约分的伏特分数保存，分母为正。
7. 样本、成功回执、工单成功状态在同一 SQLite 事务提交。写入或提交出错时传播 SQLite 异常，回滚本次全部业务写入。此时工单仍显示采集中，已核实的物理释放有效，后续可调用推进以继续处理队列。
8. 质检员在设备离线后领件，并以归档原码和冻结依据复算班次电压。报告将工单互斥地分为成功、撤单和缺采。仅成功件参与极值，按精确值比较。同值采用最早登记的样本出处。已知空班的类别均为空，无样本时两端极值均为空。

## Python 接口

- `archive.schema.open_store(path)` 打开连接，`initialize_archive(connection)` 幂等建库。传入 `':memory:'` 可建立短期存储。调用者管理连接关闭。顶层登记、发布、班次复核以及初始化使用闲置连接，其中登记和发布持有各自写事务。
- `jobs.shifts.ShiftCatalog(connection).ensure_shift(shift_id)` 登记班次，`require_shift(shift_id)` 查询已知班次。未知班次抛出 `KeyError`。
- `jobs.orders.OrderBook(connection, ranges).register(shift_id, order_id, requested_range, calibration)` 登记并返回带 `sequence` 的工单。非法标识、档位、换算依据或同班重复抛出 `ValueError`。`capture.convert.Calibration(range_name, revision, gain, offset)` 接受 `Fraction`、整数或精确数字字符串作为系数。
- `jobs.cancel.cancel(book, shift_id, order_id)` 只执行持久业务撤单。`control.dispatch.Session(connection, shift_id, ranges, device)` 的 `cancel(order_id, stop=True)` 可为已启动工单另外请求物理停止。未知工单抛出 `KeyError`，不允许的状态抛出 `ValueError`。
- `control.dispatch.advance(session, event=None)` 推进队列，可传入同一录制设备提供的下一条通知。返回本次提交的 `ArchivedSample` 或 `None`。`Session.orders`、`Session.ranges`、`Session.occupancy` 和 `Session.device` 供查询与协同操作。一次录制中的设备、控制和采集状态由同一值班会话串行推进。
- `archive.read.read_sample(connection, sample_id)` 返回 `ArchivedSample`（`sample` 及 `receipt`），未知固定号返回 `None`。固定号由班次和工单键构成。`list_shift_samples(connection, shift_id)` 按班次工单登记序返回固定件。
- `audit.report.review_shift(connection, shift_id)` 在一致的读快照中返回覆盖及带出处的 `minimum`、`maximum`。归档复算与已存值不一致时抛出 `ValueError`。

## 录制格式

`samples/day.json` 是完整输入。顶层包含录制和班次标识、支持档位、初始档位、空位数、将按下发顺序使用的具名触发，以及操作列表。操作为 `register`、`advance`、`cancel` 或 `event`。`register` 显式输入工单、档位、版次、增益及偏置，`cancel` 可携带布尔值 `stop`。

事件均有严格递增的正整数 `step`。`range_applied` 携带 `command_id` 和 `range_name`。`exposure` 指明 `trigger_id`，设备在该步记录实际档位和位置。`completed` 携带 `trigger_id`、`slot`、`exposure_step` 及 `raw_code`，可核实的完成在测量缺失或非法时也会释放原触发。`stop_accepted`、`stopped` 携带原触发与位置。`replay` 用 `of_step` 指向较早的非曝光通知，该步只重新送达原通知。设备的换档、采样、停止请求和录制事件都出现在轨迹中。
