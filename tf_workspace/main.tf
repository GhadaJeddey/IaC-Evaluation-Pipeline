resource "aws_ecr_lifecycle_policy" "backend_service_policy" {
  repository = aws_ecr_repository.backend_service.name

  policy = <<POLICY
{
  "rules": [
    {
      "rulePriority": 1,
      "description": "Expire untagged images after 1 day",
      "selection": {
        "tagStatus": "untagged",
        "countType": "sinceImagePushed",
        "countUnit": "days",
        "countNumber": 1
      },
      "action": {
        "type": "expire"
      }
    },
    {
      "rulePriority": 2,
      "description": "Retain latest 30 tagged images",
      "selection": {
        "tagStatus": "tagged",
        "tagPrefixList": ["v"]
      },
      "action": {
        "type": "expire",
        "parameters": {
          "countType": "imageCountMoreThan",
          "countNumber": 30
        }
      }
    }
  ]
}
POLICY

  tags = {
    FinOpsAction = "SET_LIFECYCLE"
    FinOpsReviewed = "true"
  }
}

# Add to existing ECR repository configuration
resource "aws_ecr_repository" "backend_service" {
  name                 = "backend-service"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = {
    Name        = "backend-service"
    Environment = "production"
  }
}