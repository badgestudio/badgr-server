from rest_framework import permissions


class HasObjectPermission(permissions.BasePermission):

    def has_object_permission(self, request, view, obj):
        object_permission_required_for_method = view.permission_map[view.method]
        permissions = obj.get_permissions(request.user)
        return permissions[object_permission_required_for_method]
