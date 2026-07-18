variable "project"           { type = string }
variable "vpc_id"            { type = string }
variable "public_subnet_ids" { type = list(string) }
variable "app_sg_id"         { type = string }
variable "db_endpoint"       { type = string }
variable "llm_secret_arn"    { type = string }
variable "db_secret_arn"     { type = string }
