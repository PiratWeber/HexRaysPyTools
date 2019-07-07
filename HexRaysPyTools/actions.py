import re
import logging

import idaapi
import idc

import HexRaysPyTools.forms as forms
import HexRaysPyTools.core.classes as classes
import HexRaysPyTools.core.type_library as type_library
from HexRaysPyTools.core.structure_graph import StructureGraph
from HexRaysPyTools.core.temporary_structure import VirtualTable, TemporaryStructureModel

logger = logging.getLogger(__name__)


def register(action, *args):
    idaapi.register_action(
        idaapi.action_desc_t(
            action.name,
            action.description,
            action(*args),
            action.hotkey
        )
    )


def unregister(action):
    idaapi.unregister_action(action.name)


class GetStructureBySize(idaapi.action_handler_t):
    # TODO: apply type automatically if expression like `var = new(size)`

    name = "my:WhichStructHaveThisSize"
    description = "Structures with this size"
    hotkey = "W"

    def __init__(self):
        idaapi.action_handler_t.__init__(self)

    @staticmethod
    def select_structure_by_size(size):
        result = type_library.choose_til()
        if result:
            selected_library, max_ordinal, is_local_type = result
            matched_types = []
            tinfo = idaapi.tinfo_t()
            for ordinal in xrange(1, max_ordinal):
                tinfo.create_typedef(selected_library, ordinal)
                if tinfo.get_size() == size:
                    name = tinfo.dstr()
                    description = idaapi.print_tinfo(None, 0, 0, idaapi.PRTYPE_DEF, tinfo, None, None)
                    matched_types.append([str(ordinal), name, description])

            type_chooser = forms.MyChoose(
                matched_types,
                "Select Type",
                [["Ordinal", 5 | idaapi.Choose2.CHCOL_HEX], ["Type Name", 25], ["Declaration", 50]],
                165
            )
            selected_type = type_chooser.Show(True)
            if selected_type != -1:
                if is_local_type:
                    return int(matched_types[selected_type][0])
                return type_library.import_type(selected_library, matched_types[selected_type][1])
        return None

    def activate(self, ctx):
        hx_view = idaapi.get_widget_vdui(ctx.widget)
        if hx_view.item.citype != idaapi.VDI_EXPR or hx_view.item.e.op != idaapi.cot_num:
            return
        ea = ctx.cur_ea
        c_number = hx_view.item.e.n
        number_value = c_number._value
        ordinal = GetStructureBySize.select_structure_by_size(number_value)
        if ordinal:
            number_format_old = c_number.nf
            number_format_new = idaapi.number_format_t()
            number_format_new.flags = idaapi.FF_1STRO | idaapi.FF_0STRO
            operand_number = number_format_old.opnum
            number_format_new.opnum = operand_number
            number_format_new.props = number_format_old.props
            number_format_new.type_name = idaapi.create_numbered_type_name(ordinal)

            c_function = hx_view.cfunc
            number_formats = c_function.numforms    # idaapi.user_numforms_t
            operand_locator = idaapi.operand_locator_t(ea, ord(operand_number) if operand_number else 0)
            if operand_locator in number_formats:
                del number_formats[operand_locator]

            number_formats[operand_locator] = number_format_new
            c_function.save_user_numforms()
            hx_view.refresh_view(True)

    def update(self, ctx):
        if ctx.widget_type == idaapi.BWN_PSEUDOCODE:
            return idaapi.AST_ENABLE_FOR_FORM
        return idaapi.AST_DISABLE_FOR_FORM


class CreateNewField(idaapi.action_handler_t):
    name = "my:CreateNewField"
    description = "Create New Field"
    hotkey = "Ctrl+F"

    def __init__(self):
        idaapi.action_handler_t.__init__(self)

    @staticmethod
    def check(cfunc, ctree_item):
        if ctree_item.citype != idaapi.VDI_EXPR:
            return

        item = ctree_item.it.to_specific_type
        if item.op not in (idaapi.cot_memptr, idaapi.cot_memref):
            return

        parent = cfunc.body.find_parent_of(ctree_item.it).to_specific_type
        if parent.op != idaapi.cot_idx or parent.y.op != idaapi.cot_num:
            idx = 0
        else:
            idx = parent.y.numval()

        struct_type = item.x.type
        struct_type.remove_ptr_or_array()

        udt_member = idaapi.udt_member_t()
        udt_member.offset = item.m * 8
        struct_type.find_udt_member(idaapi.STRMEM_OFFSET, udt_member)
        if udt_member.name[0:3] != "gap":
            return

        return struct_type, udt_member.offset // 8, idx

    def activate(self, ctx):
        hx_view = idaapi.get_widget_vdui(ctx.widget)
        result = self.check(hx_view.cfunc, hx_view.item)
        if result is None:
            return

        struct_tinfo, offset, idx = result
        ordinal = struct_tinfo.get_ordinal()
        struct_name = struct_tinfo.dstr()

        if (offset + idx) % 2:
            default_field_type = "_BYTE"
        elif (offset + idx) % 4:
            default_field_type = "_WORD"
        else:
            default_field_type = "_DWORD"

        declaration = idaapi.asktext(
            0x10000, "{0} field_{1:X}".format(default_field_type, offset + idx), "Enter new structure member:"
        )
        if declaration is None:
            return

        result = self.parse_declaration(declaration)
        if result is None:
            logger.warn("Bad member declaration")
            return

        field_tinfo, field_name = result
        field_size = field_tinfo.get_size()
        udt_data = idaapi.udt_type_data_t()
        udt_member = idaapi.udt_member_t()

        struct_tinfo.get_udt_details(udt_data)
        udt_member.offset = offset * 8
        struct_tinfo.find_udt_member(idaapi.STRMEM_OFFSET, udt_member)
        gap_size = udt_member.size // 8

        gap_leftover = gap_size - idx - field_size

        if gap_leftover < 0:
            print "[ERROR] Too big size for the field. Type with maximum {0} bytes can be used".format(gap_size - idx)
            return

        iterator = udt_data.find(udt_member)
        iterator = udt_data.erase(iterator)

        if gap_leftover > 0:
            udt_data.insert(iterator, TemporaryStructureModel.get_padding_member(offset + idx + field_size, gap_leftover))

        udt_member = idaapi.udt_member_t()
        udt_member.offset = offset * 8 + idx
        udt_member.name = field_name
        udt_member.type = field_tinfo
        udt_member.size = field_size

        iterator = udt_data.insert(iterator, udt_member)

        if idx > 0:
            udt_data.insert(iterator, TemporaryStructureModel.get_padding_member(offset, idx))

        struct_tinfo.create_udt(udt_data, idaapi.BTF_STRUCT)
        struct_tinfo.set_numbered_type(idaapi.cvar.idati, ordinal, idaapi.BTF_STRUCT, struct_name)
        hx_view.refresh_view(True)

    def update(self, ctx):
        if ctx.widget_type == idaapi.BWN_PSEUDOCODE:
            return idaapi.AST_ENABLE_FOR_FORM
        return idaapi.AST_DISABLE_FOR_FORM

    @staticmethod
    def parse_declaration(declaration):
        m = re.search(r"^(\w+[ *]+)(\w+)(\[(\d+)\])?$", declaration)
        if m is None:
            return

        type_name, field_name, _, arr_size = m.groups()
        if field_name[0].isdigit():
            print "[ERROR] Bad field name"
            return

        result = idc.ParseType(type_name, 0)
        if result is None:
            return

        _, tp, fld = result
        tinfo = idaapi.tinfo_t()
        tinfo.deserialize(idaapi.cvar.idati, tp, fld, None)
        if arr_size:
            assert tinfo.create_array(tinfo, int(arr_size))
        return tinfo, field_name


class ShowGraph(idaapi.action_handler_t):
    name = "my:ShowGraph"
    description = "Show graph"
    hotkey = "G"

    def __init__(self):
        idaapi.action_handler_t.__init__(self)
        self.graph = None
        self.graph_view = None

    def activate(self, ctx):
        """
        :param ctx: idaapi.action_activation_ctx_t
        :return:    None
        """
        form = self.graph_view.GetTForm() if self.graph_view else None
        if form:
            self.graph_view.change_selected([sel + 1 for sel in ctx.chooser_selection])
            self.graph_view.Show()
        else:
            self.graph = StructureGraph([sel + 1 for sel in ctx.chooser_selection])
            self.graph_view = forms.StructureGraphViewer("Structure Graph", self.graph)
            self.graph_view.Show()

    def update(self, ctx):
        if ctx.widget_type == idaapi.BWN_LOCTYPS:
            idaapi.attach_action_to_popup(ctx.widget, None, self.name)
            return idaapi.AST_ENABLE_FOR_FORM
        return idaapi.AST_DISABLE_FOR_FORM


class ShowClasses(idaapi.action_handler_t):

    name = "my:ShowClasses"
    description = "Classes"
    hotkey = "Alt+F1"

    def __init__(self):
        idaapi.action_handler_t.__init__(self)

    def activate(self, ctx):
        """
        :param ctx: idaapi.action_activation_ctx_t
        :return:    None
        """
        tform = idaapi.find_tform('Classes')
        if not tform:
            class_viewer = forms.ClassViewer(classes.ProxyModel(), classes.TreeModel())
            class_viewer.Show()
        else:
            idaapi.switchto_tform(tform, True)

    def update(self, ctx):
        return idaapi.AST_ENABLE_ALWAYS


class CreateVtable(idaapi.action_handler_t):

    name = "my:CreateVtable"
    description = "Create Virtual Table"
    hotkey = "V"

    def __init__(self):
        idaapi.action_handler_t.__init__(self)

    @staticmethod
    def check(ea):
        return ea != idaapi.BADADDR and VirtualTable.check_address(ea)

    def activate(self, ctx):
        ea = ctx.cur_ea
        if self.check(ea):
            vtable = VirtualTable(0, ea)
            vtable.import_to_structures(True)

    def update(self, ctx):
        if ctx.widget_type == idaapi.BWN_DISASM:
            if self.check(ctx.cur_ea):
                idaapi.attach_action_to_popup(ctx.widget, None, self.name)
                return idaapi.AST_ENABLE
            idaapi.detach_action_from_popup(ctx.widget, self.name)
            return idaapi.AST_DISABLE
        return idaapi.AST_DISABLE_FOR_FORM
