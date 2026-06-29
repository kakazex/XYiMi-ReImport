import bpy
import struct
import os
import math # Added for rotation
from configparser import ConfigParser
from bpy_extras.io_utils import ImportHelper
from bpy.props import StringProperty
from bpy.types import Operator
from pathlib import Path

# -----------------------------------------------------------------------------
# BLENDER ADDON INFO
# -----------------------------------------------------------------------------

bl_info = {
    "name": "XXMI REIMPORT",
    "author": "BOJRO ELEGANCE",
    "version": (1, 2, 0),
    "blender": (3, 0, 0),
    "location": "File > Import > Genshin Impact Mod (.ini)",
    "description": "Imports Genshin Impact character mods from 3dmigoto/GIMI format",
    "warning": "",
    "doc_url": "",
    "category": "Import-Export",
}

# -----------------------------------------------------------------------------
# BINARY FILE PARSERS
# -----------------------------------------------------------------------------

def read_position_buffer(file_path):
    """
    Parses the Position buffer.
    Stride 40 is assumed to be: Position(3f), Normal(3f), Tangent(4f).
    """
    positions, normals, tangents = [], [], []
    with open(file_path, "rb") as f:
        while chunk := f.read(40):
            if len(chunk) == 40:
                pos = struct.unpack_from("<3f", chunk, 0)
                norm = struct.unpack_from("<3f", chunk, 12)
                tan = struct.unpack_from("<4f", chunk, 24)
                positions.append(pos)
                normals.append(norm)
                tangents.append(tan)
    return positions, normals, tangents

def read_texcoord_buffer(file_path):
    """
    Parses the Texcoord buffer.
    Stride 20 is assumed to contain at least two UV sets.
    Reads UV0(2f), UV1(2f).
    """
    uv0_list, uv1_list = [], []
    with open(file_path, "rb") as f:
        while chunk := f.read(20):
            if len(chunk) == 20:
                uv0 = struct.unpack_from("<2f", chunk, 0)
                uv1 = struct.unpack_from("<2f", chunk, 8)
                uv0_list.append((uv0[0], 1.0 - uv0[1]))
                uv1_list.append((uv1[0], 1.0 - uv1[1]))
    return uv0_list, uv1_list
    
def read_blend_buffer(file_path):
    """
    Parses the Blend buffer for rigging.
    Stride 32 is assumed to be: Bone Indices(4I), Bone Weights(4f).
    """
    blend_indices, blend_weights = [], []
    with open(file_path, "rb") as f:
        while chunk := f.read(32):
            if len(chunk) == 32:
                indices = struct.unpack_from("<4I", chunk, 0)
                weights = struct.unpack_from("<4f", chunk, 16)
                blend_indices.append(indices)
                blend_weights.append(weights)
    return blend_indices, blend_weights

def read_index_buffer(file_path):
    """Parses an Index Buffer (4-byte unsigned integers)."""
    indices = []
    with open(file_path, "rb") as f:
        while chunk := f.read(4):
            if len(chunk) == 4:
                indices.append(struct.unpack("<I", chunk)[0])
    return indices

# -----------------------------------------------------------------------------
# BLENDER MESH CREATION
# -----------------------------------------------------------------------------

def create_material(obj_name, base_dir, textures):
    """Creates a new material and sets up a basic node tree with textures."""
    mat = bpy.data.materials.new(name=f"Mat_{obj_name}")
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get('Principled BSDF')
    
    def create_tex_node(node_tree, tex_path, location, label):
        if os.path.exists(tex_path):
            tex_image_node = node_tree.nodes.new('ShaderNodeTexImage')
            tex_image_node.image = bpy.data.images.load(tex_path, check_existing=True)
            tex_image_node.location = location
            tex_image_node.label = label
            return tex_image_node
        return None

    diffuse_path = os.path.join(base_dir, textures.get('ps-t0') or textures.get('ps-t1', ''))
    if diffuse_node := create_tex_node(mat.node_tree, diffuse_path, (-350, 280), "Diffuse"):
        mat.node_tree.links.new(bsdf.inputs['Base Color'], diffuse_node.outputs['Color'])

    normal_path = ""
    for tex_key in textures:
        if "NormalMap" in textures[tex_key]:
            normal_path = os.path.join(base_dir, textures[tex_key])
            break
            
    if normal_path and (normal_node := create_tex_node(mat.node_tree, normal_path, (-350, -50), "Normal")):
        normal_node.image.colorspace_settings.name = 'Non-Color'
        normal_map_node = mat.node_tree.nodes.new('ShaderNodeNormalMap')
        normal_map_node.location = (-150, -50)
        mat.node_tree.links.new(normal_map_node.inputs['Color'], normal_node.outputs['Color'])
        mat.node_tree.links.new(bsdf.inputs['Normal'], normal_map_node.outputs['Normal'])
            
    return mat

def import_model(ini_path):
    """Main function to parse the INI and build the model."""
    base_dir = os.path.dirname(ini_path)
    config = ConfigParser()
    config.read(ini_path)

    # --- 1. Dynamically find resource sections and character name ---
    pos_res, tex_res, blend_res = None, None, None
    character_name = Path(ini_path).stem
    
    override_sections = [s for s in config.sections() if s.startswith('TextureOverride')]
    if override_sections:
        common_prefix = os.path.commonprefix([s.replace('TextureOverride', '') for s in override_sections])
        if common_prefix and common_prefix[-1].isupper() and len(common_prefix) > 1:
            for i in range(len(common_prefix) -1, -1, -1):
                if common_prefix[i].islower():
                    character_name = common_prefix[:i+1]
                    break
            else:
                character_name = common_prefix
        elif common_prefix:
            character_name = common_prefix

    for section in config.sections():
        if not section.startswith('Resource'):
            continue
        if 'filename' in config[section]:
            filename = config[section]['filename']
            if 'Position.buf' in filename: pos_res = section
            if 'Texcoord.buf' in filename: tex_res = section
            if 'Blend.buf' in filename: blend_res = section
    
    if not all([pos_res, tex_res, blend_res]):
        raise Exception("Could not find all required Position, Texcoord, and Blend resource sections in the INI.")

    # --- 2. Load all shared resource buffers ---
    positions, normals, _ = read_position_buffer(os.path.join(base_dir, config[pos_res]['filename']))
    uv0, uv1 = read_texcoord_buffer(os.path.join(base_dir, config[tex_res]['filename']))
    blend_indices, blend_weights = read_blend_buffer(os.path.join(base_dir, config[blend_res]['filename']))

    # --- 3. Create a root object to parent all parts to ---
    root_obj = bpy.data.objects.new(f"{character_name}_Root", None)
    bpy.context.collection.objects.link(root_obj)

    # --- 4. Identify and build each mesh part ---
    mesh_parts = []
    for section in override_sections:
        if config.has_option(section, 'ib'):
            part_name = section.replace('TextureOverride', '').replace(character_name, '')
            ib_resource = config[section]['ib']
            ib_filename = config[ib_resource]['filename']
            textures = {k: config[section][k] for k in config[section] if k.startswith('ps-t')}
            mesh_parts.append({"name": part_name, "ib_file": ib_filename, "textures": textures})

    for part in mesh_parts:
        mesh_name = f"{character_name}_{part['name']}"
        mesh = bpy.data.meshes.new(name=mesh_name)
        obj = bpy.data.objects.new(mesh_name, mesh)
        bpy.context.collection.objects.link(obj)

        indices = read_index_buffer(os.path.join(base_dir, part['ib_file']))
        faces = [indices[i:i+3] for i in range(0, len(indices), 3)]

        mesh.from_pydata(positions, [], faces)
        mesh.update()

        uv_layer_0 = mesh.uv_layers.new(name="UV0")
        for l in mesh.loops:
            if l.vertex_index < len(uv0):
                uv_layer_0.data[l.index].uv = uv0[l.vertex_index]

        uv_layer_1 = mesh.uv_layers.new(name="UV1")
        for l in mesh.loops:
            if l.vertex_index < len(uv1):
                uv_layer_1.data[l.index].uv = uv1[l.vertex_index]

        all_bone_indices = {idx for indices_list in blend_indices for idx in indices_list}
        vertex_groups = {i: obj.vertex_groups.new(name=str(i)) for i in all_bone_indices}
        for i, vert_indices in enumerate(blend_indices):
            for j, bone_index in enumerate(vert_indices):
                if blend_weights[i][j] > 0:
                    vertex_groups[bone_index].add([i], blend_weights[i][j], 'REPLACE')
        
        mesh.polygons.foreach_set("use_smooth", [True] * len(mesh.polygons))
        mesh.normals_split_custom_set_from_vertices(normals)
        if hasattr(mesh, 'use_auto_smooth'):
            mesh.use_auto_smooth = True
        
        material = create_material(mesh_name, base_dir, part['textures'])
        obj.data.materials.append(material)

        # Set the parent to the root object
        obj.parent = root_obj
        
        mesh.update(); mesh.validate()

    # --- 5. Apply Final Rotation to the Root Object ---
    root_obj.rotation_euler.x = math.radians(90)

    print(f"Successfully imported {character_name} with {len(mesh_parts)} mesh parts.")

# -----------------------------------------------------------------------------
# BLENDER OPERATOR AND REGISTRATION
# -----------------------------------------------------------------------------

class GI_MOD_OT_Import(Operator, ImportHelper):
    """Importer for Genshin Impact mods"""
    bl_idname = "import_scene.gi_mod"
    bl_label = "Import Genshin Mod"
    bl_options = {'PRESET', 'UNDO'}

    filename_ext = ".ini"
    filter_glob: StringProperty(default="*.ini", options={'HIDDEN'})

    def execute(self, context):
        try:
            import_model(self.filepath)
        except Exception as e:
            self.report({'ERROR'}, f"Failed to import mod. See console for details. Error: {e}")
            import traceback
            traceback.print_exc()
            return {'CANCELLED'}
        return {'FINISHED'}

def menu_func_import(self, context):
    self.layout.operator(GI_MOD_OT_Import.bl_idname, text="Genshin Impact Mod (.ini)")

def register():
    bpy.utils.register_class(GI_MOD_OT_Import)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)

def unregister():
    bpy.utils.unregister_class(GI_MOD_OT_Import)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)

if __name__ == "__main__":
    register()