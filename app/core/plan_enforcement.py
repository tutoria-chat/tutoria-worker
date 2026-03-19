"""
Plan Enforcement - Subscription limit checking and feature gating

Provides utility functions to verify whether a university can create
additional resources (courses, modules) and whether specific features
are available under their active subscription plan.
"""
import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.models.university import University
from app.models.course import Course
from app.models.module import Module
from app.models.user import User
from app.models.student_course import StudentCourse
from app.models.subscription import Subscription
from app.models.plan import Plan

logger = logging.getLogger(__name__)

# Feature name to Plan column mapping
FEATURE_MAP = {
    "ai_quizzes": "has_ai_quizzes",
    "whatsapp": "has_whatsapp",
    "priority_support": "has_priority_support",
    "custom_model_config": "has_custom_model_config",
}


def _get_active_plan(db: Session, university_id: int) -> Optional[Plan]:
    """
    Retrieve the active subscription plan for a university.

    Looks for an active or trialing subscription and returns the associated Plan.
    Enterprise universities bypass plan checks entirely.

    Args:
        db: Database session.
        university_id: The university ID to look up.

    Returns:
        Optional[Plan]: The active plan, or None if no active subscription exists.
    """
    subscription = (
        db.query(Subscription)
        .filter(
            Subscription.university_id == university_id,
            Subscription.status.in_(["active", "trialing"]),
        )
        .first()
    )

    if not subscription:
        logger.debug(f"No active subscription found for university_id={university_id}")
        return None

    plan = db.query(Plan).filter(Plan.id == subscription.plan_id).first()
    return plan


def check_course_limit(db: Session, university_id: int) -> bool:
    """
    Check if a university can create more courses.

    Enterprise universities and plans with unlimited courses (max_courses=NULL)
    always return True. Otherwise, compares current course count against the
    plan limit and the university-level override.

    Args:
        db: Database session.
        university_id: The university ID to check.

    Returns:
        bool: True if the university can create another course, False if at limit.
    """
    university = db.query(University).filter(University.id == university_id).first()
    if not university:
        logger.warning(f"University id={university_id} not found")
        return False

    # Enterprise universities bypass plan limits
    if university.is_enterprise:
        return True

    # Check university-level override first
    if university.max_courses is not None:
        current_count = db.query(Course).filter(Course.university_id == university_id).count()
        allowed = current_count < university.max_courses
        if not allowed:
            logger.info(
                f"University id={university_id} at university-level course limit "
                f"({current_count}/{university.max_courses})"
            )
        return allowed

    # Check plan limit
    plan = _get_active_plan(db, university_id)
    if not plan:
        # No active plan - allow by default (graceful degradation)
        logger.debug(f"No plan found for university_id={university_id}, allowing course creation")
        return True

    if plan.max_courses is None:
        # Unlimited courses
        return True

    current_count = db.query(Course).filter(Course.university_id == university_id).count()
    allowed = current_count < plan.max_courses
    if not allowed:
        logger.info(
            f"University id={university_id} at plan course limit "
            f"({current_count}/{plan.max_courses}) on plan '{plan.slug}'"
        )
    return allowed


def check_module_limit(db: Session, university_id: int) -> bool:
    """
    Check if a university can create more modules.

    Counts all modules across all courses belonging to the university
    and compares against the plan limit or university-level override.

    Args:
        db: Database session.
        university_id: The university ID to check.

    Returns:
        bool: True if the university can create another module, False if at limit.
    """
    university = db.query(University).filter(University.id == university_id).first()
    if not university:
        logger.warning(f"University id={university_id} not found")
        return False

    # Enterprise universities bypass plan limits
    if university.is_enterprise:
        return True

    # Count modules across all university courses
    current_count = (
        db.query(Module)
        .join(Course, Module.course_id == Course.id)
        .filter(Course.university_id == university_id)
        .count()
    )

    # Check university-level override first
    if university.max_modules is not None:
        allowed = current_count < university.max_modules
        if not allowed:
            logger.info(
                f"University id={university_id} at university-level module limit "
                f"({current_count}/{university.max_modules})"
            )
        return allowed

    # Check plan limit
    plan = _get_active_plan(db, university_id)
    if not plan:
        logger.debug(f"No plan found for university_id={university_id}, allowing module creation")
        return True

    if plan.max_modules is None:
        return True

    allowed = current_count < plan.max_modules
    if not allowed:
        logger.info(
            f"University id={university_id} at plan module limit "
            f"({current_count}/{plan.max_modules}) on plan '{plan.slug}'"
        )
    return allowed


def check_student_limit(db: Session, university_id: int) -> dict:
    """
    Check if a university can enroll more students.

    Counts all unique students enrolled in any course belonging to the
    university (via the StudentCourses junction table) and compares against
    the plan limit or university-level override.

    Args:
        db: Database session.
        university_id: The university ID to check.

    Returns:
        dict: A dictionary with keys:
            - allowed (bool): Whether the university can enroll another student.
            - current (int): Current number of enrolled students.
            - limit (int | None): The effective max_students limit, or None if unlimited.
    """
    university = db.query(University).filter(University.id == university_id).first()
    if not university:
        logger.warning(f"University id={university_id} not found")
        return {"allowed": False, "current": 0, "limit": 0}

    # Enterprise universities bypass plan limits
    if university.is_enterprise:
        return {"allowed": True, "current": 0, "limit": None}

    # Count unique students enrolled in any course belonging to this university
    current_count = (
        db.query(User.user_id)
        .join(StudentCourse, User.user_id == StudentCourse.student_id)
        .join(Course, StudentCourse.course_id == Course.id)
        .filter(
            Course.university_id == university_id,
            User.user_type == "student",
        )
        .distinct()
        .count()
    )

    # Check university-level override first
    if university.max_students is not None:
        allowed = current_count < university.max_students
        if not allowed:
            logger.info(
                f"University id={university_id} at university-level student limit "
                f"({current_count}/{university.max_students})"
            )
        return {"allowed": allowed, "current": current_count, "limit": university.max_students}

    # Check plan limit
    plan = _get_active_plan(db, university_id)
    if not plan:
        logger.debug(f"No plan found for university_id={university_id}, allowing student enrollment")
        return {"allowed": True, "current": current_count, "limit": None}

    if plan.max_students is None:
        # Unlimited students
        return {"allowed": True, "current": current_count, "limit": None}

    allowed = current_count < plan.max_students
    if not allowed:
        logger.info(
            f"University id={university_id} at plan student limit "
            f"({current_count}/{plan.max_students}) on plan '{plan.slug}'"
        )
    return {"allowed": allowed, "current": current_count, "limit": plan.max_students}


def check_feature_access(db: Session, university_id: int, feature: str) -> bool:
    """
    Check if a university's plan includes a specific feature.

    Supported features:
        - 'ai_quizzes': AI-generated quiz questions
        - 'whatsapp': WhatsApp integration
        - 'priority_support': Priority support access
        - 'custom_model_config': Custom AI model configuration

    Args:
        db: Database session.
        university_id: The university ID to check.
        feature: The feature name to check (see supported features above).

    Returns:
        bool: True if the feature is available, False otherwise.

    Raises:
        ValueError: If the feature name is not recognized.
    """
    if feature not in FEATURE_MAP:
        raise ValueError(
            f"Unknown feature '{feature}'. Supported features: {list(FEATURE_MAP.keys())}"
        )

    # Enterprise universities have all features
    university = db.query(University).filter(University.id == university_id).first()
    if not university:
        logger.warning(f"University id={university_id} not found")
        return False

    if university.is_enterprise:
        return True

    plan = _get_active_plan(db, university_id)
    if not plan:
        logger.debug(
            f"No plan found for university_id={university_id}, "
            f"denying feature '{feature}'"
        )
        return False

    column_name = FEATURE_MAP[feature]
    has_feature = getattr(plan, column_name, False)

    if not has_feature:
        logger.info(
            f"Feature '{feature}' not available for university_id={university_id} "
            f"on plan '{plan.slug}'"
        )

    return bool(has_feature)


def get_university_limits(db: Session, university_id: int) -> dict:
    """
    Get a university's current usage vs plan limits.

    Returns a comprehensive dictionary with resource usage, limits,
    and feature availability for the university's active plan.

    Args:
        db: Database session.
        university_id: The university ID to query.

    Returns:
        dict: A dictionary containing:
            - plan_name: Name of the active plan (or None)
            - plan_slug: Slug of the active plan (or None)
            - is_enterprise: Whether the university is enterprise
            - courses: {current, max, unlimited}
            - modules: {current, max, unlimited}
            - features: {ai_quizzes, whatsapp, priority_support, custom_model_config}
    """
    university = db.query(University).filter(University.id == university_id).first()
    if not university:
        return {"error": f"University id={university_id} not found"}

    # Count current resources
    course_count = db.query(Course).filter(Course.university_id == university_id).count()
    module_count = (
        db.query(Module)
        .join(Course, Module.course_id == Course.id)
        .filter(Course.university_id == university_id)
        .count()
    )
    student_count = (
        db.query(User.user_id)
        .join(StudentCourse, User.user_id == StudentCourse.student_id)
        .join(Course, StudentCourse.course_id == Course.id)
        .filter(
            Course.university_id == university_id,
            User.user_type == "student",
        )
        .distinct()
        .count()
    )

    plan = _get_active_plan(db, university_id)

    # Determine effective limits (university override > plan limit)
    max_courses = university.max_courses
    max_modules = university.max_modules
    max_students = university.max_students

    if plan and max_courses is None:
        max_courses = plan.max_courses
    if plan and max_modules is None:
        max_modules = plan.max_modules
    if plan and max_students is None:
        max_students = plan.max_students

    # Determine feature access
    features = {}
    for feature_name in FEATURE_MAP:
        if university.is_enterprise:
            features[feature_name] = True
        elif plan:
            column_name = FEATURE_MAP[feature_name]
            features[feature_name] = bool(getattr(plan, column_name, False))
        else:
            features[feature_name] = False

    return {
        "plan_name": plan.name if plan else None,
        "plan_slug": plan.slug if plan else None,
        "is_enterprise": university.is_enterprise or False,
        "courses": {
            "current": course_count,
            "max": max_courses,
            "unlimited": max_courses is None,
        },
        "modules": {
            "current": module_count,
            "max": max_modules,
            "unlimited": max_modules is None,
        },
        "students": {
            "current": student_count,
            "max": max_students,
            "unlimited": max_students is None,
        },
        "features": features,
    }
