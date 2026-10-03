"""交付归档包场景测试：资格校验/整包不落库/快照冻结/幂等重放/冲突/并发一致时点。

直接压 HTTP 接口（urllib，无第三方依赖）。服务需已启动。
用法: python3 scripts/delivery_checks.py [base_url]
"""
import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
results = []


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f" :: {detail}"))


def err_code(body):
    return body.get("error", {}).get("code")


def err_cert(body):
    return body.get("error", {}).get("details", {}).get("cert_no")


TZ = timezone(timedelta(hours=8))
t0 = datetime(2026, 3, 1, 0, 0, tzinfo=TZ)


def d(days):
    return (t0 + timedelta(days=days)).isoformat()


def issue_ref(no, dev, name=None, vf=0, vt=400):
    body = {"cert_no": no, "device_id": dev,
            "valid_from": d(vf), "valid_to": d(vt), "calibrated_at": d(vf)}
    if name:
        body["device_name"] = name
    return call("POST", "/api/v1/reference-certificates", body)


def issue_cal(no, dev, parent, conclusion="PASS", name=None, vf=10, vt=300, cal=11):
    body = {"cert_no": no, "device_id": dev,
            "valid_from": d(vf), "valid_to": d(vt), "calibrated_at": d(cal),
            "conclusion": conclusion, "parent_cert_no": parent}
    if name:
        body["device_name"] = name
    return call("POST", "/api/v1/certificates", body)


def archive(pkg, nos):
    return call("POST", "/api/v1/delivery-archives",
                {"archive_no": pkg, "cert_nos": nos})


def chain_nos(item):
    return [n["certificate"]["cert_no"] for n in item.get("chain", {}).get("nodes", [])]


print("== 0. 前置：基准 + 多级普通证书（一张含更正声明） ==")
s, _ = issue_ref("DL-ROOT", "DEV-DL-ROOT", "归档基准器")
check("基准 201", s == 201, f"got {s}")
s, _ = issue_cal("DL-MID", "DEV-DL-MID", "DL-ROOT", name="中间表")
check("二级 201", s == 201, f"got {s}")
s, _ = issue_cal("DL-LEAF", "DEV-DL-LEAF", "DL-MID", name="客户表")
check("三级 201", s == 201, f"got {s}")
s, _ = issue_cal("DL-OTHER", "DEV-DL-OTHER", "DL-ROOT", name="另一张表")
check("另一张合格表 201", s == 201, f"got {s}")
s, _ = issue_cal("DL-FAIL", "DEV-DL-FAIL", "DL-ROOT", conclusion="FAIL", name="不合格表")
check("不合格表可签发 201", s == 201, f"got {s}")
s, _ = call("POST", "/api/v1/correction-declarations", {
    "declaration_no": "DL-CORR-1", "cert_no": "DL-MID",
    "corrections": {"device_name": "中间表(已更正)", "remark": "归档前更正"},
    "reason": "补型号"})
check("DL-MID 更正声明 201", s == 201, f"got {s}")

print("== A. 正常归档：按提交顺序返回原始内容/链/合并展示 ==")
s, body = archive("DL-PKG-1", ["DL-LEAF", "DL-MID"])
check("创建 201", s == 201, f"got {s} {json.dumps(body, ensure_ascii=False)[:200]}")
check("归档编号与数量", body.get("archive_no") == "DL-PKG-1" and body.get("count") == 2,
      json.dumps(body, ensure_ascii=False)[:150])
check("items 严格按提交顺序",
      [i["cert_no"] for i in body.get("items", [])] == ["DL-LEAF", "DL-MID"],
      str([i["cert_no"] for i in body.get("items", [])]))
check("含创建时刻 created_at", bool(body.get("created_at")), str(body.get("created_at")))
leaf, mid = body["items"]
check("叶子链 基准->中间->叶子",
      chain_nos(leaf) == ["DL-ROOT", "DL-MID", "DL-LEAF"], str(chain_nos(leaf)))
check("中间链 基准->中间", chain_nos(mid) == ["DL-ROOT", "DL-MID"], str(chain_nos(mid)))
check("链起点均为基准",
      leaf["chain"]["root_cert_no"] == "DL-ROOT" and mid["chain"]["root_cert_no"] == "DL-ROOT",
      "root mismatch")
check("certificate 为原始签发内容（更正不影响）",
      mid["certificate"]["device_name"] == "中间表"
      and leaf["certificate"]["device_name"] == "客户表",
      json.dumps(mid["certificate"], ensure_ascii=False)[:150])
check("链节点也是原始内容（DL-MID 节点未更正名）",
      next(n for n in leaf["chain"]["nodes"] if n["certificate"]["cert_no"] == "DL-MID")
      ["certificate"]["device_name"] == "中间表",
      "chain node mutated by correction")
check("display 为按已登记声明合并后的展示",
      mid["display"] == {"device_name": "中间表(已更正)", "operator": None,
                         "remark": "归档前更正"},
      json.dumps(mid["display"], ensure_ascii=False))
check("无更正的目标 display 取原始值",
      leaf["display"] == {"device_name": "客户表", "operator": None, "remark": None},
      json.dumps(leaf["display"], ensure_ascii=False))
check("快照内全部节点 ACTIVE/PASS",
      all(n["certificate"]["status"] == "ACTIVE" and n["certificate"]["conclusion"] == "PASS"
          for it in body["items"] for n in it["chain"]["nodes"]),
      "non-active node in snapshot")

print("== B. 同编号同有序列表重试：返回首次快照（200，逐字节一致） ==")
s, body_retry = archive("DL-PKG-1", ["DL-LEAF", "DL-MID"])
check("重试 200", s == 200, f"got {s}")
check("重试响应与首次完全一致", body_retry == body,
      json.dumps(body_retry, ensure_ascii=False)[:150])
s, body_get = call("GET", "/api/v1/delivery-archives/DL-PKG-1")
check("按编号查询 200 且与首次一致", s == 200 and body_get == body, f"got {s}")

print("== C. 同编号不同列表报冲突：不同集合 / 不同顺序 / 不同元素 ==")
s, b = archive("DL-PKG-1", ["DL-MID", "DL-LEAF"])
check("顺序不同 409 delivery_archive_conflict",
      s == 409 and err_code(b) == "delivery_archive_conflict"
      and b["error"]["details"].get("existing_cert_nos") == ["DL-LEAF", "DL-MID"],
      f"{s} {json.dumps(b, ensure_ascii=False)[:200]}")
s, b = archive("DL-PKG-1", ["DL-LEAF"])
check("元素不同 409", s == 409 and err_code(b) == "delivery_archive_conflict", f"got {s}")
s, b = archive("DL-PKG-1", ["DL-LEAF", "DL-OTHER"])
check("换元素 409", s == 409 and err_code(b) == "delivery_archive_conflict", f"got {s}")
check("冲突未落库另存（GET 仍为首次快照）",
      call("GET", "/api/v1/delivery-archives/DL-PKG-1")[1] == body, "snapshot changed")

print("== D. 未知归档包编号查询：404 ==")
s, b = call("GET", "/api/v1/delivery-archives/DL-NOPE")
check("404 delivery_archive_not_found",
      s == 404 and err_code(b) == "delivery_archive_not_found", f"got {s}")

print("== E. 列表编号重复：422，整包不保存 ==")
s, b = archive("DL-PKG-DUP", ["DL-LEAF", "DL-LEAF"])
check("重复 422 delivery_cert_duplicate",
      s == 422 and err_code(b) == "delivery_cert_duplicate" and err_cert(b) == "DL-LEAF",
      f"{s} {json.dumps(b, ensure_ascii=False)[:200]}")
s, _ = call("GET", "/api/v1/delivery-archives/DL-PKG-DUP")
check("整包不保存（编号未占用，可修正重发）", s == 404, f"got {s}")

print("== F. 目标证书不存在：404 cert_not_found，整包不保存并点名 ==")
s, b = archive("DL-PKG-MISS", ["DL-LEAF", "DL-GHOST"])
check("404 cert_not_found", s == 404 and err_code(b) == "cert_not_found"
      and err_cert(b) == "DL-GHOST", f"{s} {json.dumps(b, ensure_ascii=False)[:150]}")
s, _ = call("GET", "/api/v1/delivery-archives/DL-PKG-MISS")
check("缺失整包不保存", s == 404, f"got {s}")

print("== G. 目标为基准证书：422 delivery_target_not_calibration ==")
s, b = archive("DL-PKG-REF", ["DL-ROOT"])
check("基准目标 422", s == 422 and err_code(b) == "delivery_target_not_calibration"
      and err_cert(b) == "DL-ROOT", f"{s} {json.dumps(b, ensure_ascii=False)[:150]}")
s, b = archive("DL-PKG-REF2", ["DL-LEAF", "DL-ROOT"])
check("混合基准目标 422 且编号可查", s == 422
      and err_code(b) == "delivery_target_not_calibration" and err_cert(b) == "DL-ROOT",
      f"got {s}")

print("== H. 目标结论 FAIL：422 delivery_target_not_qualified ==")
s, b = archive("DL-PKG-FAIL", ["DL-FAIL"])
check("FAIL 目标 422", s == 422 and err_code(b) == "delivery_target_not_qualified"
      and err_cert(b) == "DL-FAIL", f"{s} {json.dumps(b, ensure_ascii=False)[:150]}")
s, b = archive("DL-PKG-FAIL2", ["DL-LEAF", "DL-FAIL"])
check("混入 FAIL 目标整包拒绝", s == 422
      and err_code(b) == "delivery_target_not_qualified" and err_cert(b) == "DL-FAIL",
      f"got {s}")

print("== I. 请求结构校验：空列表 / 缺字段 / 空编号 ==")
s, _ = call("POST", "/api/v1/delivery-archives", {"cert_nos": ["DL-LEAF"]})
check("缺归档编号 422", s == 422, f"got {s}")
s, _ = call("POST", "/api/v1/delivery-archives", {"archive_no": "DL-PKG-X"})
check("缺列表 422", s == 422, f"got {s}")
s, _ = call("POST", "/api/v1/delivery-archives",
            {"archive_no": "DL-PKG-X", "cert_nos": []})
check("空列表 422", s == 422, f"got {s}")
s, _ = call("POST", "/api/v1/delivery-archives",
            {"archive_no": "DL-PKG-X", "cert_nos": [""]})
check("空编号 422", s == 422, f"got {s}")

print("== J. 快照冻结：撤销/更正后重放仍为首次快照 ==")
# 再登记一条更正（改变 DL-MID 当前展示）
s, _ = call("POST", "/api/v1/correction-declarations", {
    "declaration_no": "DL-CORR-2", "cert_no": "DL-MID",
    "corrections": {"device_name": "中间表(二次更正)"}, "reason": "再改"})
check("二次更正 201", s == 201, f"got {s}")
# 撤销基准，级联失效全部下游
s, rev = call("POST", "/api/v1/certificates/DL-ROOT/revoke",
              {"reason": "归档后撤销", "operator": "qa"})
check("撤销基准 200", s == 200, f"got {s}")
s, cur = call("GET", "/api/v1/certificates/DL-LEAF")
check("现实状态：叶子级联失效 INVALID", s == 200 and cur.get("status") == "INVALID",
      cur.get("status", ""))
# 重放旧包
s, body_after = archive("DL-PKG-1", ["DL-LEAF", "DL-MID"])
check("撤销后重放仍 200", s == 200, f"got {s}")
check("重放与首次快照逐字节一致（撤销+更正均不影响）", body_after == body,
      json.dumps(body_after, ensure_ascii=False)[:150])
frozen_mid = next(i for i in body_after["items"] if i["cert_no"] == "DL-MID")
check("快照 display 停留在归档时刻（仅一次更正）",
      frozen_mid["display"]["device_name"] == "中间表(已更正)",
      frozen_mid["display"]["device_name"])
check("快照链节点仍全 ACTIVE",
      all(n["certificate"]["status"] == "ACTIVE"
          for it in body_after["items"] for n in it["chain"]["nodes"]),
      "snapshot status changed")
# 当前更正视图反映二次更正，快照不反映
s, live = call("GET", "/api/v1/certificates/DL-MID/corrections")
check("当前更正视图反映二次更正（与快照分离）",
      live["display"]["device_name"] == "中间表(二次更正)",
      live["display"]["device_name"])

print("== K. 撤销后不允许新建包含失效目标/失效上游的归档包 ==")
s, b = archive("DL-PKG-DEAD1", ["DL-OTHER"])
check("失效目标 422 delivery_target_not_active",
      s == 422 and err_code(b) == "delivery_target_not_active"
      and err_cert(b) == "DL-OTHER", f"{s} {json.dumps(b, ensure_ascii=False)[:150]}")

print("== L. 全新活跃链：撤销后重签，归档正常 ==")
s, _ = issue_ref("DL-R2", "DEV-DL-R2", "新基准")
check("重签基准 201", s == 201, f"got {s}")
s, _ = issue_cal("DL-C2", "DEV-DL-C2", "DL-R2", name="新客户表")
check("重签普通证书 201", s == 201, f"got {s}")
s, b = archive("DL-PKG-2", ["DL-C2"])
check("新链归档 201", s == 201, f"got {s}")
check("新链内容正确",
      chain_nos(b["items"][0]) == ["DL-R2", "DL-C2"]
      and b["items"][0]["certificate"]["status"] == "ACTIVE",
      json.dumps(b, ensure_ascii=False)[:150])

print("== M. 并发同编号同列表：恰好一次首次归档，其余重放同一快照 ==")
outcomes = []
lock = threading.Lock()


def worker_m(i):
    s, b = archive("DL-PKG-C1", ["DL-C2"])
    with lock:
        outcomes.append((i, s, b))


threads = [threading.Thread(target=worker_m, args=(i,)) for i in range(6)]
[t.start() for t in threads]
[t.join() for t in threads]
codes = sorted(o[1] for o in outcomes)
check("6 并发：1 个 201 + 5 个 200", codes == [200] * 5 + [201], f"codes={codes}")
distinct = {json.dumps(o[2], sort_keys=True, ensure_ascii=False) for o in outcomes}
check("全部并发响应内容一致（同一快照）", len(distinct) == 1, f"distinct={len(distinct)}")

print("== N. 并发同编号不同列表：一次成功，其余冲突 ==")
# 两个互不相同但各自合格的列表（DL-C2 与 DL-C3 都是 ACTIVE/PASS 的 CALIBRATION）。
# 资格校验只在“查无已存在编号”时进行；首个提交者落库后，另一列表一律 409。
s, _ = issue_cal("DL-C3", "DEV-DL-C3", "DL-R2", name="另一张新客户表")
check("第二张合格目标就绪", s == 201, f"got {s}")
outcomes2 = []


def worker_n(i):
    nos = ["DL-C2"] if i % 2 == 0 else ["DL-C3"]
    s, b = archive("DL-PKG-C2", nos)
    with lock:
        outcomes2.append((i, s, b))


threads = [threading.Thread(target=worker_n, args=(i,)) for i in range(6)]
[t.start() for t in threads]
[t.join() for t in threads]
created = [o for o in outcomes2 if o[1] == 201]
conflicts = [o for o in outcomes2 if o[1] == 409]
replays = [o for o in outcomes2 if o[1] == 200]
# 关键不变量：恰好一个不同列表真正首次落库（仅一个 201）；其余事务要么
# 重放“与已存列表相同”的请求（200，且列表必须与赢家一致），要么因列表不同
# 而冲突（409）。两个列表互不相容，因此 200 重放者必然与赢家同属一个列表。
check("恰好一个事务首次落库（仅一个 201）", len(created) == 1,
      f"codes={sorted(o[1] for o in outcomes2)}")
winner_list = ["DL-C2"] if created[0][0] % 2 == 0 else ["DL-C3"]
check("201 赢家与冲突计数合理（其余为 200/409，无 422/500）",
      len(created) + len(conflicts) + len(replays) == 6
      and all(err_code(o[2]) == "delivery_archive_conflict" for o in conflicts),
      json.dumps([(o[0], o[1], err_code(o[2])) for o in outcomes2], ensure_ascii=False))
check("所有 200 重放者返回的都是赢家列表的同一份快照",
      all(o[2] == created[0][2] for o in replays),
      "replay body differs from winner snapshot")
s, stored = call("GET", "/api/v1/delivery-archives/DL-PKG-C2")
stored_nos = [i["cert_no"] for i in stored["items"]]
check("库内仅存赢家那一份列表", s == 200 and stored_nos == winner_list,
      f"stored={stored_nos} winner={winner_list}")
# 两个列表彼此都应被判冲突（输家列表现在必然 409）
loser_list = ["DL-C3"] if winner_list == ["DL-C2"] else ["DL-C2"]
s, b = archive("DL-PKG-C2", loser_list)
check("输家列表随后提交确定 409", s == 409 and err_code(b) == "delivery_archive_conflict",
      f"got {s} {err_code(b)}")

print("== O. 并发 撤销 ↔ 归档：归档整包对应同一一致时点，不混合新旧 ==")
# 一条独立的活跃链供争抢
s, _ = issue_ref("DL-R3", "DEV-DL-R3", "并发基准")
s, _ = issue_cal("DL-K", "DEV-DL-K", "DL-R3", name="并发客户表")
check("并发用前置链就绪", s == 201, f"got {s}")

barrier = threading.Barrier(2)
race_archive = []
race_revoke = []


def archive_worker():
    barrier.wait()
    race_archive.append(archive("DL-PKG-RACE", ["DL-K"]))


def revoke_worker():
    barrier.wait()
    race_revoke.append(call("POST", "/api/v1/certificates/DL-R3/revoke",
                            {"reason": "并发撤销", "operator": "qa"}))


t1 = threading.Thread(target=archive_worker)
t2 = threading.Thread(target=revoke_worker)
t1.start(); t2.start()
t1.join(); t2.join()
ac, ab = race_archive[0]
rc, rb = race_revoke[0]
check("撤销请求本身成功（200）", rc == 200, f"revoke={rc} {json.dumps(rb, ensure_ascii=False)[:120]}")
if ac in (200, 201):
    # 归档先于撤销提交：快照必须整张一致（链上全部 ACTIVE），重放冻结不变
    nodes = [n for it in ab["items"] for n in it["chain"]["nodes"]]
    check("归档成功且快照全部 ACTIVE（取撤销前时点）",
          all(n["certificate"]["status"] == "ACTIVE" for n in nodes),
      f"archive={ac} statuses={[n['certificate']['status'] for n in nodes]}")
    s, replay = archive("DL-PKG-RACE", ["DL-K"])
    check("成功归档可重放且仍冻结 ACTIVE", s == 200 and replay == ab, f"replay={s}")
else:
    check("归档晚于撤销：干净拒绝（422 target/upstream not active，非 500）",
          ac == 422 and err_code(ab) in (
              "delivery_target_not_active", "delivery_upstream_not_active"),
          f"archive={ac} {err_code(ab)} {json.dumps(ab, ensure_ascii=False)[:150]}")
    s, _ = call("GET", "/api/v1/delivery-archives/DL-PKG-RACE")
    check("被拒归档未保存", s == 404, f"got {s}")

print("== P. 原有接口保持兼容（证书详情/链/更正视图/批次） ==")
s, b = call("GET", "/api/v1/certificates/DL-MID")
check("证书详情仍为原始签发字段",
      s == 200 and b["device_name"] == "中间表" and b["kind"] == "CALIBRATION",
      json.dumps(b, ensure_ascii=False)[:120])
s, b = call("GET", "/api/v1/certificates/DL-MID/chain")
check("追溯链仍可用且为原始内容",
      s == 200 and [n["certificate"]["cert_no"] for n in b["nodes"]] == ["DL-ROOT", "DL-MID"],
      f"got {s}")
s, b = call("POST", "/api/v1/certificate-batches", {
    "batch_key": "DL-BAT",
    "certificates": [
        {"kind": "REFERENCE", "cert_no": "DL-BAT-ROOT", "device_id": "DEV-DL-BAT",
         "valid_from": d(0), "valid_to": d(400), "calibrated_at": d(0)},
        {"kind": "CALIBRATION", "cert_no": "DL-BAT-C", "device_id": "DEV-DL-BAT-C",
         "valid_from": d(10), "valid_to": d(300), "calibrated_at": d(11),
         "conclusion": "PASS", "parent_cert_no": "DL-BAT-ROOT"},
    ]})
check("批次签发仍正常 201", s == 201, f"got {s}")
s, b = call("GET", "/api/v1/certificate-batches/DL-BAT")
check("批次快照查询仍正常 200", s == 200 and b.get("count") == 2, f"got {s}")

print("== Q. 归档只读快照在数据库层封印（UPDATE/DELETE 拒绝） ==")
# 通过 API 无法直接验证 DB 触发器；此处确认快照查询在大量操作后内容仍稳定
s, b = call("GET", "/api/v1/delivery-archives/DL-PKG-1")
check("PKG-1 快照始终可读且数量/顺序不变",
      s == 200 and [i["cert_no"] for i in b["items"]] == ["DL-LEAF", "DL-MID"],
      f"got {s}")

fails = [r for r in results if not r[1]]
print(f"\n结果: PASS={len(results) - len(fails)} FAIL={len(fails)}")
sys.exit(1 if fails else 0)
