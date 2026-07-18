variable "project"            { type = string }
variable "vpc_id"             { type = string }
variable "private_subnet_ids" { type = list(string) }
variable "db_sg_id"           { type = string }
variable "db_secret_arn"      { type = string }
