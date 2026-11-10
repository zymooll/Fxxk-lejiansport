"""LeGym 3.10.3 wire format, checked offline against the supplied capture."""
import base64
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone

FIELD_MAP = (
    ('rt', 'type'), ('lcs', 'gpsMileage'), ('bs', 'paceNumber'), ('bf', 'paceRange'),
    ('zlc', 'totalMileage'), ('tp', 'totalPart'), ('em', 'effectiveMileage'),
    ('ep', 'effectivePart'), ('uer', 'uneffectiveReason'), ('st', 'startTime'),
    ('et', 'endTime'), ('kill', 'calorie'), ('ap', 'avePace'), ('xq', 'semesterId'),
    ('jf', 'scoringType'), ('lid', 'limitationsGoalsSexInfoId'), ('sv', 'systemVersion'),
    ('app', 'appVersion'), ('dt', 'deviceType'), ('kt', 'keepTime'),
)
# Current LeGym business timestamps are Beijing time (UTC+08:00).
SHANGHAI = timezone(timedelta(hours=8))


def java_double(value):
    # Wire values used here are bounded ordinary decimal doubles, not exponents.
    value = float(value)
    if not math.isfinite(value): raise ValueError('签名字段必须为有限数值')
    return str(value)


def digest(payload):
    material = (java_double(payload['effectiveMileage']) + str(int(payload['effectivePart'])) + payload['startTime']
                + str(int(payload['calorie'])) + str(int(payload['avePace'])) + str(int(payload['keepTime']))
                + str(int(payload['paceNumber'])) + java_double(payload['totalMileage']) + str(int(payload['totalPart'])))
    return hashlib.sha1((material+'itauVfnexHiRigZ6').encode('utf-8')).hexdigest()


def cjson_number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError('签名数值字段无效')
    if value == int(value) and -2147483648 <= value <= 2147483647:
        return str(int(value))
    short = format(value, '.15g')
    return short if float(short) == value else format(value, '.17g')


def oct_plaintext(payload):
    rows = []
    for alias, name in FIELD_MAP:
        value = payload[name]
        text = json.dumps(value, ensure_ascii=False) if isinstance(value, str) else cjson_number(value)
        rows.append('\t'+json.dumps(alias)+':\t'+text)
    return '{\n'+',\n'.join(rows)+'\n}'


def aes_key(user_id, school_id):
    if not isinstance(user_id, str) or not isinstance(school_id, str) or len(user_id) < 12 or len(school_id) < 7:
        raise ValueError('真实用户或学校标识不完整，不能签名')
    # NativeLib.getRnKey -> getRunningKey -> Arrays.copyOf(UTF_8, 16).
    return (user_id[3:6]+school_id[4:7]+user_id[9:12]+'3e0783d6891a4a3e9521dcb6bb341560').encode('utf-8')[:16]


def sign(payload, user_id, school_id):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7
    payload = dict(payload)
    payload['signDigital'] = digest(payload)
    end = datetime.strptime(payload['endTime'], '%Y-%m-%d %H:%M:%S')
    payload['signTime'] = (end+timedelta(seconds=int(payload['keepTime']) % 11)).strftime('%Y-%m-%d %H:%M:%S')
    padding = PKCS7(128).padder()
    padded = padding.update(oct_plaintext(payload).encode('utf-8'))+padding.finalize()
    cipher = Cipher(algorithms.AES(aes_key(user_id, school_id)), modes.ECB()).encryptor()
    # Android Base64.DEFAULT wraps at 76 columns and adds a final newline.
    payload['oct'] = base64.encodebytes(cipher.update(padded)+cipher.finalize()).decode('ascii')
    return payload


def build_simulation(plan, created_at, snapshot, profile, now, *, rebase_to_upload=False):
    if not rebase_to_upload and plan['config']['clockScale'] != 1:
        raise ValueError('真实服务器联调只支持1倍真实计时；加速测试请保存到本地')
    if not plan.get('isSimulated'):
        raise ValueError('此入口仅用于明确标记的模拟联调')
    end = datetime.fromisoformat(now).astimezone(SHANGHAI)
    keep_time = math.floor(plan['durationSeconds']+.5)
    if keep_time <= 0:
        raise ValueError('轨迹计划时长必须大于0')
    start = end-timedelta(seconds=keep_time) if rebase_to_upload else datetime.fromisoformat(created_at).astimezone(SHANGHAI)
    if not rebase_to_upload and (end-start).total_seconds()+1 < keep_time:
        raise ValueError('真实计时尚未完成')
    miles = round(plan['observedDistanceMeters']/1000, 4)
    if miles <= 0: raise ValueError('模拟里程无效')
    limit = snapshot['runningLimit']
    if not limit.get('hasRule') or not limit.get('limitationsGoalsSexInfoId'):
        raise ValueError('真实服务没有可用于联调的当前跑步规则')
    scoring_type = int(limit['scoringType'])
    total_part = 0.0
    if scoring_type == 2:
        try: paragraph_mileage = float(limit['effectiveParagraphMileage'])
        except (KeyError, TypeError, ValueError):
            raise ValueError('当前段数规则缺少每段里程，无法构造上传请求') from None
        if not math.isfinite(paragraph_mileage) or paragraph_mileage <= 0:
            raise ValueError('当前每段里程规则无效')
        total_part = miles/paragraph_mileage
    weight = profile.get('weight')
    try: weight = float(weight)
    except (ValueError, TypeError): weight = 60
    if not math.isfinite(weight) or not 20 <= weight <= 300: weight = 60
    payload = dict(appVersion='3.10.3', deviceType='OnePlus GM1900', systemVersion='11',
                   startTime=start.strftime('%Y-%m-%d %H:%M:%S'), endTime=end.strftime('%Y-%m-%d %H:%M:%S'),
                   endTimeDate=int(end.timestamp()*1000), keepTime=keep_time,
                   gpsMileage=miles, totalMileage=miles, effectiveMileage=miles,
                   effectivePart=1 if scoring_type == 1 else int(total_part), totalPart=total_part,
                   paceNumber=0, paceRange=0, avePace=float(int(keep_time*1000/miles)), calorie=int(weight*miles),
                   semesterId=snapshot['semester']['id'], limitationsGoalsSexInfoId=limit['limitationsGoalsSexInfoId'],
                   scoringType=scoring_type, type='范围跑', uneffectiveReason='', signPoint=[],
                   routineLine=[{k: p[k] for k in ('latitude', 'longitude')} for p in plan['points']])
    return sign(payload, profile['id'], profile['schoolId'])
