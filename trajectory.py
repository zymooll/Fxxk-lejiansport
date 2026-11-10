"""Deterministic, explicitly synthetic position samples for local testing."""
import math
import random
import secrets

EARTH = 6371008.8
INTERVAL = 390 / 442
LAP_DURATION = INTERVAL * 150
MIN_LAPS = 1
MAX_LAPS = 60

def distance(a, b):
    p, q = math.radians(a['latitude']), math.radians(b['latitude'])
    h = math.sin((q-p)/2)**2 + math.cos(p)*math.cos(q)*math.sin(math.radians(b['longitude']-a['longitude'])/2)**2
    return 2*EARTH*math.asin(min(1, math.sqrt(max(0, h))))

def length(points):
    return sum(distance(a, b) for a, b in zip(points, points[1:]))

def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

def settings(data):
    if not isinstance(data, dict):
        raise ValueError('设置必须为 JSON 对象')
    laps = data.get('laps', 3)
    scale = data.get('clockScale', 30)
    sigma = data.get('sigmaMeters', .6)
    cap = data.get('maxOffsetMeters', 2)
    correlation = data.get('correlationSeconds', 4)
    seed = data.get('seed', 20261105)
    if not isinstance(laps, int) or isinstance(laps, bool) or not MIN_LAPS <= laps <= MAX_LAPS:
        raise ValueError(f'圈数应为 {MIN_LAPS}–{MAX_LAPS} 的整数')
    if not number(scale) or scale not in (1, 30, 60):
        raise ValueError('时钟倍率仅支持 1、30、60')
    if not number(sigma) or not 0 <= sigma <= 2:
        raise ValueError('测试偏移标准差应在 0–2 米之间')
    if not number(cap) or not 0 <= cap <= 5 or cap < sigma:
        raise ValueError('最大偏移应在标准差到 5 米之间')
    if not number(correlation) or not .5 <= correlation <= 30:
        raise ValueError('相关时间应为 0.5–30 秒')
    if not isinstance(seed, int) or isinstance(seed, bool) or not 0 <= seed <= 2**31-1:
        raise ValueError('种子应为 0–2147483647 的整数')
    return dict(laps=laps, clockScale=scale, sigmaMeters=float(sigma), maxOffsetMeters=float(cap),
                correlationSeconds=float(correlation), seed=seed)

def generate(reference, config, *, rng=None):
    cfg = settings(config)
    if len(reference) != 151 or reference[0] != reference[-1]:
        raise ValueError('参考线应为 151 个点且首尾闭合')
    rng = rng if rng is not None else random.Random(cfg['seed'])
    alpha = math.exp(-INTERVAL/cfg['correlationSeconds'])
    innovation = cfg['sigmaMeters'] * math.sqrt(1-alpha*alpha)
    north = east = 0.0
    points = []
    for i in range(150*cfg['laps'] + 1):
        base = reference[i % 150]
        if i:
            north = alpha*north + innovation*rng.gauss(0, 1)
            east = alpha*east + innovation*rng.gauss(0, 1)
            norm = math.hypot(north, east)
            if norm > cfg['maxOffsetMeters'] and norm:
                north *= cfg['maxOffsetMeters']/norm
                east *= cfg['maxOffsetMeters']/norm
        point = dict(latitude=base['latitude']+math.degrees(north/EARTH),
                     longitude=base['longitude']+math.degrees(east/(EARTH*math.cos(math.radians(base['latitude'])))),
                     elapsedMs=round(i*INTERVAL*1000, 3),
                     accuracyMeters=round(max(1, cfg['sigmaMeters']*2), 3), source='simulated-local')
        if i == 0:
            point['latitude'], point['longitude'] = reference[0]['latitude'], reference[0]['longitude']
        points.append(point)
    lap_meters = length(reference)
    return {'isSimulated': True, 'config': cfg, 'points': points, 'sampleIntervalSeconds': INTERVAL,
            'durationSeconds': cfg['laps']*LAP_DURATION, 'pointCount': len(points),
            'referenceLapMeters': lap_meters, 'estimatedDistanceKm': round(lap_meters*cfg['laps']/1000, 3),
            'referenceDistanceMeters': lap_meters*cfg['laps'], 'observedDistanceMeters': length(points),
            'firstPointPreserved': points[0]['latitude'] == reference[0]['latitude'] and points[0]['longitude'] == reference[0]['longitude']}


def secure_settings(config):
    if not isinstance(config, dict) or any(key not in ('laps', 'sigmaMeters', 'maxOffsetMeters', 'correlationSeconds', 'seed') for key in config):
        raise ValueError('仅接受圈数、轨迹偏移及种子设置')
    seed = config.get('seed', -1)
    if not isinstance(seed, int) or isinstance(seed, bool) or not -1 <= seed <= 2**31-1:
        raise ValueError('种子应为 -1 或 0–2147483647 的整数')
    normalized = settings({**config, 'clockScale': 1, 'seed': max(seed, 0)})
    normalized['seed'] = seed
    normalized.pop('clockScale')
    return normalized


def generate_secure(reference, config):
    config = secure_settings(config)
    seed = config['seed']
    # -1 draws directly from the OS CSPRNG; explicit seeds reproduce a trajectory.
    rng = secrets.SystemRandom() if seed == -1 else random.Random(seed)
    plan = generate(reference, {**config, 'clockScale': 1, 'seed': seed if seed >= 0 else 0}, rng=rng)
    plan['config']['seed'] = seed
    plan['config'].pop('clockScale')
    plan['entropySource'] = 'os-csprng' if seed == -1 else 'seeded-prng'
    plan['executionMode'] = 'generate-only'
    return plan
