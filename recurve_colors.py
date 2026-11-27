"""Transfer named face-vertex colors through Recurve's corner correspondence."""
import maya.api.OpenMaya as om


class ColorTransfer:
    def __init__(self, fn):
        self.sets = {}
        self.current = fn.currentColorSetName() if fn.numColorSets else None
        counts = list(fn.getVertices()[0]) if fn.numColorSets else []
        self.counts = counts
        self.offsets = []
        offset = 0
        for count in counts:
            self.offsets.append(offset)
            offset += count
        for name in fn.getColorSetNames():
            ids = []
            for face, count in enumerate(counts):
                ids.extend(fn.getColorIndex(face,corner,name) for corner in range(count))
            self.sets[name] = {
                'colors': [tuple(c) for c in fn.getColors(name)], 'source': ids,
                'rep': fn.getColorRepresentation(name), 'clamped': fn.isColorClamped(name),
                'ids': [], 'cache': {}}

    def sample(self, name, face, a, b, weight):
        record = self.sets[name]
        offset = self.offsets[face]
        ia, ib = record['source'][offset+a], record['source'][offset+b]
        if a == b or weight <= 1e-12:return ia
        if weight >= 1-1e-12:return ib
        if ia == ib:return ia
        # An unpainted corner is absence of data, not a black or white color.
        if ia < 0 or ib < 0:return -1
        key = (ia,ib,round(weight,12)) if ia <= ib else (ib,ia,round(1-weight,12))
        cache, colors = record['cache'], record['colors']
        if key not in cache:
            cache[key] = len(colors)
            colors.append(tuple(x+(y-x)*weight for x,y in zip(colors[ia],colors[ib])))
        return cache[key]

    def add(self, face, a, b, weight):
        for name, record in self.sets.items():
            record['ids'].append(self.sample(name,face,a,b,weight))

    def apply(self, data):
        if not self.sets:return data
        # Creating/color-selecting sets requires a DAG mesh. The staging node
        # owns only detached preview data and never touches the user's history.
        selection = om.MGlobal.getActiveSelectionList()
        modifier = om.MDagModifier()
        transform = modifier.createNode('transform')
        modifier.renameNode(transform,'_recurveColorBuild#')
        modifier.newPlugValueBool(om.MFnDependencyNode(transform).findPlug('visibility',False),False)
        modifier.doIt()
        try:
            staged = om.MFnMesh()
            staged.copy(data,transform)
            for name, record in self.sets.items():
                staged.createColorSet(name,record['clamped'],record['rep'])
                if record['colors']:
                    staged.setColors(om.MColorArray(record['colors']),name,record['rep'])
                    staged.assignColors(record['ids'],name)
            if self.current in self.sets:staged.setCurrentColorSetName(self.current)
            result = om.MFnMeshData().create()
            om.MFnMesh().copy(staged.object(),result)
        finally:
            modifier.undoIt()
            om.MGlobal.setActiveSelectionList(selection)
        if list(om.MFnMesh(result).getColorSetNames()) != list(self.sets):
            raise RuntimeError('Recurve could not preserve the vertex color set names.')
        return result


def transfer_bulk(fn, data, pieces, splice, ints):
    """Keep unchanged faces' color IDs and splice only the rebuilt corners."""
    transfer = ColorTransfer(fn)
    for name, record in transfer.sets.items():
        replacements = {
            face: [[transfer.sample(name,face,a,b,w) for _,a,b,w in part] for part in parts]
            for face, parts in pieces.items()}
        _, ids = splice(ints(record['source']),ints(transfer.counts),replacements)
        record['ids'] = ids.tolist()
    return transfer.apply(data)


def sync_source_colors(node, data):
    """Populate the owned source's color plugs, which Maya uses for scene I/O."""
    fn = om.MFnMesh(data)
    if not fn.numColorSets:return
    transfer = ColorTransfer(fn)
    dep = om.MFnDependencyNode(node)
    modifier = om.MDGModifier()
    array = dep.findPlug('colorSet',False)
    for index, (name, record) in enumerate(transfer.sets.items()):
        element = array.elementByLogicalIndex(index)
        modifier.newPlugValueString(element.child(dep.attribute('colorName')),name)
        modifier.newPlugValueBool(element.child(dep.attribute('clamped')),record['clamped'])
        modifier.newPlugValueInt(element.child(dep.attribute('representation')),record['rep'])
    modifier.newPlugValueString(dep.findPlug('currentColorSet',False),transfer.current or '')
    modifier.doIt()
    out = om.MFnMesh(node)
    for name, record in transfer.sets.items():
        out.clearColors(name)
        if record['colors']:
            out.setColors(om.MColorArray(record['colors']),name,record['rep'])
            out.assignColors(record['source'],name)


def same_colors(a, b):
    """Only reuse upstream history if it also owns the evaluated color data."""
    if list(a.getColorSetNames()) != list(b.getColorSetNames()):return False
    if not a.numColorSets:return True
    if a.currentColorSetName() != b.currentColorSetName():return False
    left, right = ColorTransfer(a), ColorTransfer(b)
    for name, x in left.sets.items():
        y = right.sets[name]
        if (x['rep'],x['clamped'],x['source']) != (y['rep'],y['clamped'],y['source']):return False
        channels = (3,) if x['rep'] == a.kAlpha else range(3) if x['rep'] == a.kRGB else range(4)
        if [tuple(c[i] for i in channels) for c in x['colors']] != [tuple(c[i] for i in channels) for c in y['colors']]:return False
    return True
