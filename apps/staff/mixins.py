class PermissionedModelMixin(object):
    """
    Abstract class used for inheritance by all the Models (Badgeclass, Issuer, Faculty & Institution that have a related
    Staff model. Used for retrieving permissions and staff members.
    """

    def _get_local_permissions(self, user):
        """
        :param user: BadgeUser (teacher)
        :return: a permissions dictionary for the instance only, without looking higher in the hierarchy.
        """
        staff = self.get_staff_member(user)
        if staff:
            return staff.permissions
        else:
            return None

    def get_permissions(self, user):
        """
        This method returns (inherited or local) permissions for the instance by climbing the permission tree.
        :param user: BadgeUser (teacher)
        :return: a permissions dictionary
        """
        try:
            parent_perms = self.parent.get_permissions(user)
            local_perms = self._get_local_permissions(user)
            if not parent_perms:
                return local_perms
            elif not local_perms:
                return parent_perms
            else:
                combined_perms = {}
                for key in local_perms:
                    combined_perms[key] = local_perms[key] if local_perms[key] > parent_perms[key] else parent_perms[key]
                return combined_perms
        except AttributeError:  # recursive base case
            return self._get_local_permissions(user)

    @property
    def staff_items(self):
        return self.cached_staff

    def get_local_staff_members(self, permissions=None):
        """
        gets the staff members belonging to this object that have all of the permissions given
        :param permissions: array of permissions required
        :return: list of staff memberships that have this
        """
        result = []
        if permissions:
            for staff in self.staff_items:
                has_perms = []
                for perm in permissions:
                    if staff.permissions[perm]:
                        has_perms.append(perm)
                if len(has_perms) == len(permissions):
                    result.append(staff)
            return result
        else:
            return self.staff_items

    def get_staff_member(self, user):
        """
        Get a staff membership object belonging to the given user.
        :param user: BadgeUser (teacher)
        :return: Staff object
        """
        for staff in self.staff_items:
            if staff.user == user:
                return staff
