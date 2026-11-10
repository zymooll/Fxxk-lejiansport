"""Inclusive planar point-in-polygon for small local test regions."""
def inside_polygon(point, polygon):
    x,y=point['longitude'],point['latitude']
    inside=False
    for a,b in zip(polygon,polygon[1:]+polygon[:1]):
        ax,ay=a['longitude'],a['latitude'];bx,by=b['longitude'],b['latitude']
        cross=(x-ax)*(by-ay)-(y-ay)*(bx-ax)
        if abs(cross)<=1e-12 and min(ax,bx)-1e-10<=x<=max(ax,bx)+1e-10 and min(ay,by)-1e-10<=y<=max(ay,by)+1e-10:
            return True
        if (ay>y)!=(by>y) and x < ax+(y-ay)*(bx-ax)/(by-ay):inside=not inside
    return inside

def evaluate(points, region):
    inside=sum(any(inside_polygon(point,polygon) for polygon in region['polygons']) and
               not any(inside_polygon(point,hole) for hole in region.get('excludedPolygons',[])) for point in points)
    total=len(points)
    # Integer comparison makes the inclusive 70% threshold exact.
    return {'insidePoints':inside,'outsidePoints':total-inside,'totalPoints':total,
            'ratio':inside/total if total else 0,'requiredRatio':.70,'passed':total>0 and inside*10>=total*7,
            'boundaryCountsAsInside':True,'metric':'sample-point-count'}
